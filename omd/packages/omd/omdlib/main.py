#!/usr/bin/env python3
#
#       U  ___ u  __  __   ____
#        \/"_ \/U|' \/ '|u|  _"\
#        | | | |\| |\/| |/| | | |
#    .-,_| |_| | | |  | |U| |_| |\
#     \_)-\___/  |_|  |_| |____/ u
#          \\   <<,-,,-.   |||_
#         (__)   (./  \.) (__)_)
#
# This file is part of OMD - The Open Monitoring Distribution.
# The official homepage is at <http://omdistro.org>.
#
# OMD  is  free software;  you  can  redistribute it  and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the  Free Software  Foundation  in  version 2.  OMD  is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# ails.  You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.
"""The command line tool specific implementations of the omd command and main entry point"""

from __future__ import annotations

import abc
import errno
import fcntl
import io
import logging
import os
import pty
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping
from enum import auto, Enum
from pathlib import Path
from typing import assert_never, BinaryIO, cast, Final, IO, Literal, NamedTuple, NoReturn
from uuid import uuid4

import psutil

import omdlib
import omdlib.backup
import omdlib.certs
import omdlib.utils
from omdlib.config_hooks import (
    call_hook,
    ConfigHook,
    ConfigHookChoices,
    ConfigHooks,
    create_config_environment,
    hook_exists,
    load_config_hooks,
    load_defaults,
    load_hook_dependencies,
    save_site_conf,
    sort_hooks,
)
from omdlib.console import ok, show_success
from omdlib.contexts import AbstractSiteContext, RootContext, SiteContext
from omdlib.dialog import (
    ask_user_choices,
    dialog_config_choice_has_error,
    dialog_menu,
    dialog_message,
    dialog_regex,
    dialog_yesno,
    user_confirms,
)
from omdlib.init_scripts import call_init_scripts, check_status
from omdlib.skel_permissions import Permissions, read_skel_permissions, skel_permissions_file_path
from omdlib.system_apache import (
    delete_apache_hook,
    has_old_apache_hook_in_site,
    is_apache_hook_up_to_date,
    register_with_system_apache,
    unregister_from_system_apache,
)
from omdlib.tmpfs import (
    add_to_fstab,
    mark_tmpfs_initialized,
    prepare_tmpfs,
    remove_from_fstab,
    restore_tmpfs_dump,
    save_tmpfs_dump,
    tmpfs_mounted,
    unmount_tmpfs,
)
from omdlib.type_defs import CommandOptions, Config, ConfigChoiceHasError, Replacements
from omdlib.users_and_groups import (
    find_processes_of_user,
    group_exists,
    group_id,
    groupdel,
    switch_to_site_user,
    user_exists,
    user_id,
    user_logged_in,
    user_verify,
    useradd,
    userdel,
)
from omdlib.utils import chdir, delete_user_file
from omdlib.version_info import VersionInfo

import cmk.utils.log
import cmk.utils.tty as tty
from cmk.utils.certs import cert_dir, CN_TEMPLATE, root_cert_path, RootCA
from cmk.utils.crypto.password import Password
from cmk.utils.crypto.password_hashing import hash_password
from cmk.utils.exceptions import MKTerminate
from cmk.utils.licensing.helper import get_instance_id_file_path, save_instance_id
from cmk.utils.log import VERBOSE
from cmk.utils.paths import mkbackup_lock_dir
from cmk.utils.resulttype import Error, OK, Result
from cmk.utils.version import Version, versions_compatible, VersionsIncompatible

Arguments = list[str]
ConfigChangeCommands = list[tuple[str, str]]

cmk.utils.log.setup_console_logging()
logger = logging.getLogger("cmk.omd")


class StateMarkers:
    good = " " + tty.green + tty.bold + "*" + tty.normal
    warn = " " + tty.bgyellow + tty.black + tty.bold + "!" + tty.normal
    error = " " + tty.bgred + tty.white + tty.bold + "!" + tty.normal


def bail_out(message: str) -> NoReturn:
    sys.exit(message)


# Is used to duplicate output from stdout/stderr to a logfiles. This
# is e.g. used during "omd update" to have a chance to analyze errors
# during past updates
# TODO: Replace this with regular logging mechanics
class Log(io.StringIO):
    def __init__(self, fd: int, logfile: str) -> None:
        super().__init__()
        self.log = open(logfile, "a", encoding="utf-8")  # pylint: disable=consider-using-with
        self.fd = fd

        if self.fd == 1:
            self.orig = sys.stdout
            sys.stdout = self
        else:
            self.orig = sys.stderr
            sys.stderr = self

        self.color_replace = re.compile("\033\\[\\d{1,2}m", re.UNICODE)

    def __del__(self) -> None:
        if self.fd == 1:
            sys.stdout = self.orig
        else:
            sys.stderr = self.orig
        self.log.close()

    # TODO: Ensure we get Text here
    def write(self, data: str) -> int:
        text = data
        self.orig.write(text)
        self.log.write(self.color_replace.sub("", text))
        return len(text)

    def flush(self) -> None:
        self.log.flush()
        self.orig.flush()


g_stdout_log: Log | None = None
g_stderr_log: Log | None = None


def start_logging(logfile: str) -> None:
    global g_stdout_log, g_stderr_log
    g_stdout_log = Log(1, logfile)
    g_stderr_log = Log(2, logfile)


def stop_logging() -> None:
    global g_stdout_log, g_stderr_log
    g_stdout_log = None
    g_stderr_log = None


class CommandType(Enum):
    create = auto()
    move = auto()
    copy = auto()

    # reuse in options or not:
    restore_existing_site = auto()
    restore_as_new_site = auto()

    @property
    def short(self) -> str:
        if self is CommandType.create:
            return "create"

        if self is CommandType.move:
            return "mv"

        if self is CommandType.copy:
            return "cp"

        if self in [CommandType.restore_as_new_site, CommandType.restore_existing_site]:
            return "restore"

        raise TypeError()


# .
#   .--Sites---------------------------------------------------------------.
#   |                        ____  _ _                                     |
#   |                       / ___|(_) |_ ___  ___                          |
#   |                       \___ \| | __/ _ \/ __|                         |
#   |                        ___) | | ||  __/\__ \                         |
#   |                       |____/|_|\__\___||___/                         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Helper functions for dealing with sites                             |
#   '----------------------------------------------------------------------'


def site_name() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def is_root() -> bool:
    return os.getuid() == 0


def all_sites() -> Iterable[str]:
    basedir = os.path.join(omdlib.utils.omd_base_path(), "omd/sites")
    return sorted([s for s in os.listdir(basedir) if os.path.isdir(os.path.join(basedir, s))])


def start_site(version_info: VersionInfo, site: SiteContext) -> None:
    prepare_and_populate_tmpfs(version_info, site)
    call_init_scripts(site.dir, "start")
    if not (instance_id_file_path := get_instance_id_file_path(Path(site.dir))).exists():
        # Existing sites may not have an instance ID yet. After an update we create a new one.
        save_instance_id(file_path=instance_id_file_path, instance_id=uuid4())


def stop_if_not_stopped(site: SiteContext) -> None:
    if not site.is_stopped():
        stop_site(site)


def stop_site(site: SiteContext) -> None:
    call_init_scripts(site.dir, "stop")


def get_skel_permissions(skel_path: str, perms: Permissions, relpath: str) -> int:
    try:
        return perms[relpath]
    except KeyError:
        return get_file_permissions(f"{skel_path}/{relpath}")


def get_file_permissions(path: str) -> int:
    try:
        return os.stat(path).st_mode & 0o7777
    except Exception:
        return 0


def get_file_owner(path: str) -> str | None:
    try:
        return pwd.getpwuid(os.stat(path).st_uid)[0]
    except Exception:
        return None


def create_version_symlink(site: SiteContext, version: str) -> None:
    linkname = site.dir + "/version"
    if os.path.lexists(linkname):
        os.remove(linkname)
    os.symlink("../../versions/%s" % version, linkname)


def calculate_admin_password(options: CommandOptions) -> Password:
    if pw := options.get("admin-password"):
        return Password(pw)
    return Password.random(12)


def set_admin_password(site: SiteContext, pw: Password) -> None:
    with open("%s/etc/htpasswd" % site.dir, "w") as f:
        f.write("cmkadmin:%s\n" % hash_password(pw))


def create_skeleton_files(site: SiteContext, directory: str) -> None:
    replacements = site.replacements
    # Hack: exclude tmp if dir is '.'
    exclude_tmp = directory == "."
    skelroot = "/omd/versions/%s/skel" % omdlib.__version__
    with chdir(skelroot):  # make relative paths
        for dirpath, dirnames, filenames in os.walk(directory):
            dirpath = dirpath.removeprefix("./")
            for entry in dirnames + filenames:
                if exclude_tmp:
                    if dirpath == "." and entry == "tmp":
                        continue
                    if dirpath == "tmp" or dirpath.startswith("tmp/"):
                        continue
                create_skeleton_file(skelroot, site.dir, dirpath + "/" + entry, replacements)


def save_version_meta_data(site: SiteContext, version: str) -> None:
    """Make meta information from the version available in the site directory

    the prurpose of this metadir is to be able to upgrade without the old
    version and the symlinks

    Currently it holds the following information
    A) A copy of the versions skel/ directory
    B) A copy of the skel.permissions file
    C) A version file containing the version number of the meta data
    """
    try:
        shutil.rmtree(site.version_meta_dir)
    except FileNotFoundError:
        pass

    skelroot = "/omd/versions/%s/skel" % version
    shutil.copytree(skelroot, "%s/skel" % site.version_meta_dir, symlinks=True)

    shutil.copy(skel_permissions_file_path(version), "%s/skel.permissions" % site.version_meta_dir)

    with open("%s/version" % site.version_meta_dir, "w") as f:
        f.write("%s\n" % version)


def create_skeleton_file(
    skelbase: str, userbase: str, relpath: str, replacements: Replacements
) -> None:
    skel_path = Path(skelbase, relpath)
    user_path = Path(userbase, relpath)

    # Remove old version, if existing (needed during update)
    if user_path.exists():
        delete_user_file(str(user_path))

    # Create directories, symlinks and files
    if skel_path.is_symlink():
        user_path.symlink_to(skel_path.readlink())
    elif skel_path.is_dir():
        user_path.mkdir(parents=True)
    else:
        user_path.write_bytes(replace_tags(skel_path.read_bytes(), replacements))

    if not skel_path.is_symlink():
        mode = read_skel_permissions().get(relpath.removeprefix("./"))
        if mode is None:
            if skel_path.is_dir():
                mode = 0o750
            else:
                mode = 0o640
        user_path.chmod(mode)


def prepare_and_populate_tmpfs(version_info: VersionInfo, site: SiteContext) -> None:
    prepare_tmpfs(version_info, site)

    if not os.listdir(site.tmp_dir):
        create_skeleton_files(site, "tmp")
        chown_tree(site.tmp_dir, site.name)
        mark_tmpfs_initialized(site)
        restore_tmpfs_dump(site)

    _create_livestatus_tcp_socket_link(site)


def chown_tree(directory: str, user: str) -> None:
    uid = pwd.getpwnam(user).pw_uid
    gid = pwd.getpwnam(user).pw_gid
    os.chown(directory, uid, gid)
    for dirpath, dirnames, filenames in os.walk(directory):
        for entry in dirnames + filenames:
            os.lchown(dirpath + "/" + entry, uid, gid)


def try_chown(filename: str, user: str) -> None:
    if os.path.exists(filename):
        try:
            uid = pwd.getpwnam(user).pw_uid
            gid = pwd.getpwnam(user).pw_gid
            os.chown(filename, uid, gid)
        except Exception as e:
            sys.stderr.write(f"Cannot chown {filename} to {user}: {e}\n")


# Walks all files in the skeleton dir to execute a function for each file
#
# When called with a path in 'exclude_if_in' then paths existing relative to
# that are skipped. This is used for a second run during the update-process: to handle
# files that have vanished in the new version.
#
# The option 'relbase' is optional. It can contain a relative path which can be used
# as base for the walk instead of walking the whole tree.
def walk_skel(
    root: str,
    conflict_mode: str,
    depth_first: bool,
    exclude_if_in: str | None = None,
    relbase: str = ".",
) -> Iterable[str]:
    with chdir(root):
        # Note: os.walk first finds level 1 directories, then deeper
        # layers. If we need a real depth search instead, where we first
        # handle deep directories and files, then the top level ones.
        walk_entries = list(os.walk(relbase))
        if depth_first:
            walk_entries.reverse()

        for dirpath, dirnames, filenames in walk_entries:
            if dirpath.startswith("./"):
                dirpath = dirpath[2:]
            if dirpath.startswith("tmp"):
                continue

            # In depth first search first handle files, then directories
            if depth_first:
                entries = filenames + dirnames
            else:
                entries = dirnames + filenames
            for entry in entries:
                path = dirpath + "/" + entry
                if path.startswith("./"):
                    path = path[2:]

                if exclude_if_in and os.path.exists(exclude_if_in + "/" + path):
                    continue

                yield path


# .
#   .--omd update----------------------------------------------------------.
#   |                           _                   _       _              |
#   |        ___  _ __ ___   __| |  _   _ _ __   __| | __ _| |_ ___        |
#   |       / _ \| '_ ` _ \ / _` | | | | | '_ \ / _` |/ _` | __/ _ \       |
#   |      | (_) | | | | | | (_| | | |_| | |_) | (_| | (_| | ||  __/       |
#   |       \___/|_| |_| |_|\__,_|  \__,_| .__/ \__,_|\__,_|\__\___|       |
#   |                                    |_|                               |
#   +----------------------------------------------------------------------+
#   |  Complex handling of skeleton and user files during update           |
#   '----------------------------------------------------------------------'


# Change site specific information in files originally create from
# skeleton files. Skip files below tmp/
def patch_skeleton_files(conflict_mode: str, old_site: SiteContext, new_site: SiteContext) -> None:
    skelroot = "/omd/versions/%s/skel" % omdlib.__version__
    with chdir(skelroot):  # make relative paths
        for dirpath, _dirnames, filenames in os.walk("."):
            if dirpath.startswith("./"):
                dirpath = dirpath[2:]
            targetdir = new_site.dir + "/" + dirpath
            if targetdir.startswith(new_site.tmp_dir):
                continue  # Skip files below tmp
            for fn in filenames:
                # Skip some not patchable files that can be found in our standard skel
                if _is_unpatchable_file(fn):
                    continue

                src = dirpath + "/" + fn
                dst = targetdir + "/" + fn
                if (
                    os.path.isfile(src) and not os.path.islink(src) and os.path.exists(dst)
                ):  # not deleted by user
                    try:
                        patch_template_file(conflict_mode, src, dst, old_site, new_site)
                    except MKTerminate:
                        raise
                    except Exception as e:
                        sys.stderr.write(f"Error patching template file '{dst}': {e}\n")


def _is_unpatchable_file(path: str) -> bool:
    return path.endswith(".png") or path.endswith(".pdf")


def patch_template_file(  # pylint: disable=too-many-branches
    conflict_mode: str, src: str, dst: str, old_site: SiteContext, new_site: SiteContext
) -> None:
    # Create patch from old instantiated skeleton file to new one
    content = Path(src).read_bytes()
    for site in [old_site, new_site]:
        filename = Path(f"{dst}.skel.{site.name}")
        filename.write_bytes(replace_tags(content, site.replacements))
        try_chown(str(filename), new_site.name)

    # If old and new skeleton file are identical, then do nothing
    old_orig_path = Path(f"{dst}.skel.{old_site.name}")
    new_orig_path = Path(f"{dst}.skel.{new_site.name}")
    if old_orig_path.read_text() == new_orig_path.read_text():
        old_orig_path.unlink()
        new_orig_path.unlink()
        return

    # Now create a patch from old to new and immediately apply on
    # existing - possibly user modified - file.

    result = os.system(  # nosec B605 # BNS:2b5952
        "diff -u %s %s | %s/bin/patch --force --backup --forward --silent %s"
        % (old_orig_path, new_orig_path, new_site.dir, dst)
    )

    try_chown(dst, new_site.name)
    try_chown(dst + ".rej", new_site.name)
    try_chown(dst + ".orig", new_site.name)
    if result == 0:
        sys.stdout.write(StateMarkers.good + " Converted      %s\n" % src)
    else:
        # Make conflict resolution interactive - similar to omd update
        options = [
            ("diff", "Show conversion patch, that I've tried to apply"),
            ("you", "Show your changes compared with the original default version"),
            ("edit", "Edit half-converted file (watch out for >>>> and <<<<)"),
            ("try again", "Edit your original file and try again"),
            ("keep", "Keep half-converted version of the file"),
            ("restore", "Restore your original version of the file"),
            ("install", "Install the default version of the file"),
            (
                "brute",
                f"Simply replace /{old_site.name}/ with /{new_site.name}/ in that file",
            ),
            ("shell", "Open a shell for looking around"),
            ("abort", "Stop here and abort!"),
        ]

        while True:
            if conflict_mode in ["abort", "install"]:
                choice = conflict_mode
            elif conflict_mode == "keepold":
                choice = "restore"
            else:
                choice = ask_user_choices(
                    "Conflicts in " + src + "!",
                    "I've tried to merge your changes with the renaming of %s into %s.\n"
                    "Unfortunately there are conflicts with your changes. \n"
                    "You have the following options: " % (old_site.name, new_site.name),
                    options,
                )

            if choice == "abort":
                bail_out("Renaming aborted.")
            elif choice == "keep":
                break
            elif choice == "edit":
                with subprocess.Popen(
                    [get_editor(), dst],
                ):
                    pass
            elif choice == "diff":
                os.system(
                    f"diff -u {old_orig_path} {new_orig_path}{pipe_pager()}"
                )  # nosec B605 # BNS:2b5952
            elif choice == "brute":
                os.system(  # nosec B605 # BNS:2b5952
                    f"sed 's@/{old_site.name}/@/{new_site.name}/@g' {dst}.orig > {dst}"
                )
                changed = len(
                    [
                        l
                        for l in os.popen(
                            f"diff {dst}.orig {dst}"
                        ).readlines()  # nosec B605 # BNS:2b5952
                        if l.startswith(">")
                    ]
                )
                if changed == 0:
                    sys.stdout.write("Found no matching line.\n")
                else:
                    sys.stdout.write(
                        "Did brute-force replace, changed %s%d%s lines:\n"
                        % (tty.bold, changed, tty.normal)
                    )
                    with subprocess.Popen(["diff", "-u", dst + ".orig", dst]):
                        pass
                    break
            elif choice == "you":
                os.system(
                    f"pwd ; diff -u {old_orig_path} {dst}.orig{pipe_pager()}"
                )  # nosec B605 # BNS:2b5952
            elif choice == "restore":
                os.rename(dst + ".orig", dst)
                sys.stdout.write("Restored your version.\n")
                break
            elif choice == "install":
                os.rename(new_orig_path, dst)
                sys.stdout.write("Installed default file (with site name %s).\n" % new_site.name)
                break
            elif choice == "shell":
                relname = src.split("/")[-1]
                sys.stdout.write(" %-35s the half-converted file\n" % (relname,))
                sys.stdout.write(" %-35s your original version\n" % (relname + ".orig"))
                sys.stdout.write(" %-35s the failed parts of the patch\n" % (relname + ".rej"))
                sys.stdout.write(
                    " %-35s default version with the old site name\n"
                    % (relname + ".skel.%s" % old_site.name)
                )
                sys.stdout.write(
                    " %-35s default version with the new site name\n"
                    % (relname + ".skel.%s" % new_site.name)
                )

                sys.stdout.write("\n Starting BASH. Type CTRL-D to continue.\n\n")
                thedir = "/".join(dst.split("/")[:-1])
                os.system(
                    f"su - {new_site.name} -c 'cd {thedir} ; bash -i'"
                )  # nosec B605 # BNS:2b5952
    # remove unnecessary files
    try:
        os.remove(dst + ".skel." + old_site.name)
        os.remove(dst + ".skel." + new_site.name)
        os.remove(dst + ".orig")
        os.remove(dst + ".rej")
    except Exception:
        pass


# Try to merge changes from old->new version and
# old->user version
def merge_update_file(  # pylint: disable=too-many-branches
    site: SiteContext, conflict_mode: str, relpath: str, old_version: str, new_version: str
) -> None:
    fn = tty.bold + relpath + tty.normal

    user_path = Path(site.dir, relpath)
    permissions = user_path.stat().st_mode

    if _try_merge(site, conflict_mode, relpath, old_version, new_version) == 0:
        # ACHTUNG: Hier müssen die Dateien $DATEI-alt, $DATEI-neu und $DATEI.orig
        # gelöscht werden
        sys.stdout.write(StateMarkers.good + " Merged         %s\n" % fn)
        return

    # No success. Should we try merging the users' changes onto the new file?
    # user_patch = os.popen(
    merge_message = " (watch out for >>>>> and <<<<<)"
    editor = get_editor()
    reject_file = Path(f"{user_path}.rej")

    options = [
        ("diff", "Show differences between the new default and your version"),
        ("you", "Show your changes compared with the old default version"),
        ("new", f"Show what has changed from {old_version} to {new_version}"),
    ]
    if reject_file.exists():  # missing if patch has --merge
        options.append(("missing", "Show which changes from the update have not been merged"))
    options += [
        ("edit", "Edit half-merged file%s" % merge_message),
        ("try again", "Edit your original file and try again"),
        ("keep", "Keep half-merged version of the file"),
        ("restore", "Restore your original version of the file"),
        ("install", "Install the new default version"),
        ("shell", "Open a shell for looking around"),
        ("abort", "Stop here and abort update!"),
    ]

    while True:
        if conflict_mode in ["install", "abort"]:
            choice = conflict_mode
        elif conflict_mode == "keepold":
            choice = "restore"
        else:
            choice = ask_user_choices(
                "Conflicts in " + relpath + "!",
                "I've tried to merge the changes from version %s to %s into %s.\n"
                "Unfortunately there are conflicts with your changes. \n"
                "You have the following options: " % (old_version, new_version, relpath),
                options,
            )

        if choice == "abort":
            bail_out("Update aborted.")
        elif choice == "keep":
            break
        elif choice == "edit":
            with subprocess.Popen([editor, user_path]):
                pass
        elif choice == "diff":
            os.system(
                f"diff -u {user_path}.orig {user_path}-{new_version}{pipe_pager()}"
            )  # nosec B605 # BNS:2b5952
        elif choice == "you":
            os.system(
                f"diff -u {user_path}-{old_version} {user_path}.orig{pipe_pager()}"
            )  # nosec B605 # BNS:2b5952
        elif choice == "new":
            os.system(  # nosec B605 # BNS:2b5952
                "diff -u %s-%s %s-%s%s"
                % (user_path, old_version, user_path, new_version, pipe_pager())
            )
        elif choice == "missing":
            if reject_file.exists():
                sys.stdout.write(tty.bgblue + tty.white + reject_file.read_text() + tty.normal)
            else:
                sys.stdout.write("File %s not found.\n" % reject_file)

        elif choice == "shell":
            relname = relpath.split("/")[-1]
            sys.stdout.write(" %-25s: the current half-merged file\n" % relname)
            sys.stdout.write(
                " %-25s: the default version of %s\n" % (relname + "." + old_version, old_version)
            )
            sys.stdout.write(
                " %-25s: the default version of %s\n" % (relname + "." + new_version, new_version)
            )
            sys.stdout.write(" %-25s: your original version\n" % (relname + ".orig"))
            if reject_file.exists():
                sys.stdout.write(" %-25s: changes that haven't been merged\n" % relname + ".rej")

            sys.stdout.write("\n Starting BASH. Type CTRL-D to continue.\n\n")
            os.system("cd '%s' ; bash -i" % user_path.parent)  # nosec B605 # BNS:2b5952
        elif choice == "restore":
            Path(f"{user_path}.orig").rename(user_path)
            user_path.chmod(permissions)
            sys.stdout.write("Restored your version.\n")
            break
        elif choice == "try again":
            Path(f"{user_path}.orig").rename(user_path)
            with subprocess.Popen([editor, user_path]):
                pass
            if _try_merge(site, conflict_mode, relpath, old_version, new_version) == 0:
                sys.stdout.write(
                    "Successfully merged changes from %s -> %s into %s\n"
                    % (old_version, new_version, fn)
                )
                return
            sys.stdout.write(" Merge failed again.\n")

        else:  # install
            Path(f"{user_path}-{new_version}").rename(user_path)
            user_path.chmod(permissions)
            sys.stdout.write("Installed default file of version %s.\n" % new_version)
            break

    # Clean up temporary files
    for p in [
        f"{user_path}-{old_version}",
        f"{user_path}-{new_version}",
        "%s.orig" % user_path,
        "%s.rej" % user_path,
    ]:
        try:
            os.remove(p)
        except Exception:
            pass


def _try_merge(
    site: SiteContext, conflict_mode: str, relpath: str, old_version: str, new_version: str
) -> int:
    user_path = Path(site.dir, relpath)

    for version, skelroot in [
        (old_version, site.version_skel_dir),
        (new_version, "/omd/versions/%s/skel" % new_version),
    ]:
        p = Path(skelroot, relpath)
        while True:
            try:
                skel_content = p.read_bytes()
                break
            except Exception:
                # Do not ask the user in non-interactive mode.
                if conflict_mode in ["abort", "install"]:
                    bail_out(f"Skeleton file '{p}' of version {version} not readable.")
                elif conflict_mode == "keepold" or not user_confirms(
                    site,
                    conflict_mode,
                    "Skeleton file of version %s not readable" % version,
                    "The file '%s' is not readable for the site user. "
                    "This is most probably due a bug in release 0.42. "
                    "You can either fix that problem by making the file "
                    "readable with doing as root: chmod +r '%s' "
                    "or assume the file as empty. In that case you might "
                    "damage your configuration file "
                    "in case you have made changes to it in your site. What shall we do?" % (p, p),
                    relpath,
                    "retry",
                    "Retry reading the file (after you've fixed it)",
                    "ignore",
                    "Assume the file to be empty",
                ):
                    skel_content = b""
                    break
        Path(f"{user_path}-{version}").write_bytes(replace_tags(skel_content, site.replacements))
    version_patch = os.popen(  # nosec B605 # BNS:2b5952
        f"diff -u {user_path}-{old_version} {user_path}-{new_version}"
    ).read()

    # First try to merge the changes in the version into the users' file
    f = os.popen(  # nosec B605 # BNS:2b5952
        "%s/bin/patch --force --backup --forward --silent --merge %s >/dev/null"
        % (site.dir, user_path),
        "w",
    )
    f.write(version_patch)
    status = f.close()
    if status:
        return status // 256
    return 0


# Compares two files and returns infos wether the file type or contants have changed """
def file_status(site: SiteContext, source_path: str, target_path: str) -> tuple[bool, bool, bool]:
    source_type = filetype(source_path)
    target_type = filetype(target_path)

    if source_type == "file":
        source_content = file_contents(site, source_path)

    if target_type == "file":
        target_content = file_contents(site, target_path)

    changed_type = source_type != target_type
    # FIXME: Was ist, wenn aus einer Datei ein Link gemacht wurde? Oder umgekehrt?
    changed_content = (
        source_type == "file" and target_type == "file" and source_content != target_content
    ) or (
        source_type == "link"
        and target_type == "link"
        and os.readlink(source_path) != os.readlink(target_path)
    )
    changed = changed_type or changed_content

    return (changed_type, changed_content, changed)


def _execute_update_file(
    relpath: str,
    site: SiteContext,
    conflict_mode: str,
    old_version: str,
    new_version: str,
    new_edition: str,
    old_perms: Permissions,
) -> None:
    todo = True
    while todo:
        try:
            update_file(
                relpath, site, conflict_mode, old_version, new_version, new_edition, old_perms
            )
            todo = False
        except MKTerminate:
            raise
        except Exception:
            todo = False
            sys.stderr.write(StateMarkers.error * 40 + "\n")
            sys.stderr.write(StateMarkers.error + " Exception      %s\n" % (relpath))
            sys.stderr.write(
                StateMarkers.error
                + " "
                + traceback.format_exc().replace("\n", "\n" + StateMarkers.error + " ")
                + "\n"
            )
            sys.stderr.write(StateMarkers.error * 40 + "\n")

            # If running in interactive mode ask the user to terminate or retry
            # In case of non interactive mode just throw the exception
            if conflict_mode == "ask":
                options = [
                    ("retry", "Retry the operation"),
                    ("continue", "Continue with next files"),
                    ("abort", "Stop here and abort update!"),
                ]
                choice = ask_user_choices(
                    "Problem occured",
                    "We detected an exception (printed above). You have the "
                    "chance to fix things and retry the operation now.",
                    options,
                )
                if choice == "abort":
                    bail_out("Update aborted.")
                elif choice == "retry":
                    todo = True  # Try again


def update_file(  # pylint: disable=too-many-branches
    relpath: str,
    site: SiteContext,
    conflict_mode: str,
    old_version: str,
    new_version: str,
    to_edition: str,
    old_perms: Permissions,
) -> None:
    old_skel = site.version_skel_dir
    new_skel = "/omd/versions/%s/skel" % new_version

    ignored_prefixes = [
        # We removed dokuwiki from the OMD packages with 2.0.0i1. To prevent users from
        # accidentally removing configs or their dokuwiki content, we skip the questions to
        # remove the dokuwiki files here.
        "etc/dokuwiki",
        "var/dokuwiki",
        "local/share/dokuwiki",
    ]
    for prefix in ignored_prefixes:
        if relpath.startswith(prefix):
            sys.stdout.write(f"{StateMarkers.good} Keeping your   {relpath}\n")
            return

    replacements = site.replacements
    # omd_version of the site still contains the old version/edition at this point, make sure new
    # edition is provided
    replacements["###EDITION###"] = to_edition

    old_path = old_skel + "/" + relpath
    new_path = new_skel + "/" + relpath
    user_path = site.dir + "/" + relpath

    old_type = filetype(old_path)
    new_type = filetype(new_path)
    user_type = filetype(user_path)

    # compare our new version with the user's version
    _type_differs, _content_differs, differs = file_status(site, user_path, new_path)

    # compare our old version with the user's version
    user_changed_type, user_changed_content, user_changed = file_status(site, old_path, user_path)

    # compare our old with our new version
    _we_changed_type, _we_changed_content, we_changed = file_status(site, old_path, new_path)

    non_empty_directory = (
        not os.path.islink(user_path) and os.path.isdir(user_path) and bool(os.listdir(user_path))
    )

    #     if global_opts.verbose:
    #         sys.stdout.write("%s%s%s:\n" % (tty.bold, relpath, tty.normal))
    #         sys.stdout.write("  you       : %s\n" % user_type)
    #         sys.stdout.write("  %-10s: %s\n" % (old_version, old_type))
    #         sys.stdout.write("  %-10s: %s\n" % (new_version, new_type))

    # A --> MISSING FILES

    # Handle cases with missing files first. At least old or new are present,
    # or this function would never have been invoked.
    fn = tty.bold + tty.bgblue + tty.white + relpath + tty.normal
    fn = tty.bold + relpath + tty.normal

    # 1) New version ships new skeleton file -> simply install
    if not old_type and not user_type:
        create_skeleton_file(new_skel, site.dir, relpath, replacements)
        sys.stdout.write(StateMarkers.good + " Installed %-4s %s\n" % (new_type, fn))

    # 2) new version ships new skeleton file, but user's own file/directory/link
    #    is in the way.
    # 2a) the users file is identical with our new version
    elif not old_type and not differs:
        sys.stdout.write(StateMarkers.good + " Identical new  %s\n" % fn)

    # 2b) user's file has a different content or type
    elif not old_type:
        if user_confirms(
            site,
            conflict_mode,
            "Conflict at " + relpath,
            "The new version ships the %s %s, "
            "but you have created a %s in that place "
            "yourself. Shall we keep your %s or replace "
            "is with my %s?" % (new_type, relpath, user_type, user_type, new_type),
            relpath,
            "keep",
            "Keep your %s" % user_type,
            "replace",
            f"Replace your {user_type} with the new default {new_type}",
        ):
            sys.stdout.write(StateMarkers.warn + " Keeping your   %s\n" % fn)
        else:
            create_skeleton_file(new_skel, site.dir, relpath, replacements)
            sys.stdout.write(StateMarkers.good + " Installed %-4s %s\n" % (new_type, fn))

    # 3) old version had a file which has vanished in new (got obsolete). If the user
    #    has deleted it himself, we are just happy
    elif not new_type and not user_type:
        sys.stdout.write(StateMarkers.good + " Obsolete       %s\n" % fn)

    # 3b) same, but user has not deleted and changed type
    elif not new_type and user_changed_type:
        if user_confirms(
            site,
            conflict_mode,
            "Obsolete file " + relpath,
            "The %s %s has become obsolete in "
            "this version, but you have changed it into a "
            "%s. Do you want to keep your %s or "
            "may I remove it for you, please?" % (old_type, relpath, user_type, user_type),
            relpath,
            "keep",
            "Keep your %s" % user_type,
            "remove",
            "Remove it",
        ):
            sys.stdout.write(StateMarkers.warn + " Keeping your   %s\n" % fn)
        else:
            delete_user_file(user_path)
            sys.stdout.write(StateMarkers.warn + " Removed        %s\n" % fn)

    # 3c) same, but user has changed it contents
    elif not new_type and user_changed_content:
        if user_confirms(
            site,
            conflict_mode,
            f"Changes in obsolete {old_type} {relpath}",
            "The %s %s has become obsolete in "
            "the new version, but you have changed its contents. "
            "Do you want to keep your %s or "
            "may I remove it for you, please?" % (old_type, relpath, user_type),
            relpath,
            "keep",
            "keep your %s, though it is obsolete" % user_type,
            "remove",
            "remove your %s" % user_type,
        ):
            sys.stdout.write(StateMarkers.warn + " Keeping your   %s\n" % fn)
        else:
            delete_user_file(user_path)
            sys.stdout.write(StateMarkers.warn + " Removed        %s\n" % fn)

    # 3d) same, but it is a directory which is not empty
    elif not new_type and non_empty_directory:
        if user_confirms(
            site,
            conflict_mode,
            "Non empty obsolete directory %s" % (relpath),
            "The directory %s has become obsolete in "
            "the new version, but you have contents in it. "
            "Do you want to keep your directory or "
            "may I remove it for you, please?" % (relpath),
            relpath,
            "keep",
            "keep your directory, though it is obsolete",
            "remove",
            "remove your directory",
        ):
            sys.stdout.write(StateMarkers.warn + " Keeping your   %s\n" % fn)
        else:
            delete_user_file(user_path)
            sys.stdout.write(StateMarkers.warn + " Removed        %s\n" % fn)

    # 3e) same, but user hasn't changed anything -> silently delete
    elif not new_type:
        delete_user_file(user_path)
        sys.stdout.write(StateMarkers.good + " Vanished       %s\n" % fn)

    # 4) old and new exist, but user file not. User has deleted that
    #    file. We simply do nothing in that case. The user surely has
    #    a good reason why he deleted the file.
    elif not user_type and not we_changed:
        sys.stdout.write(
            StateMarkers.good + " Unwanted       %s (unchanged, removed by you)\n" % fn
        )

    # 4b) File changed in new version. Simply warn if user has deleted it.
    elif not user_type:
        sys.stdout.write(StateMarkers.warn + " Missing        %s\n" % fn)

    # B ---> UNCHANGED, EASY CASES

    # 5) New version didn't change anything -> no need to update
    elif not we_changed:
        pass

    # 6) User didn't change anything -> take over new version
    elif not user_changed:
        create_skeleton_file(new_skel, site.dir, relpath, replacements)
        sys.stdout.write(StateMarkers.good + " Updated        %s\n" % fn)

    # 7) User changed, but accidentally exactly as we did -> no action necessary
    elif not differs:
        sys.stdout.write(StateMarkers.good + " Identical      %s\n" % fn)

    # TEST UNTIL HERE

    # C ---> PATCH DAY, HANDLE FILES
    # 7) old, new and user are files. And all are different
    elif old_type == "file" and new_type == "file" and user_type == "file":
        try:
            merge_update_file(site, conflict_mode, relpath, old_version, new_version)
        except KeyboardInterrupt:
            raise
        except MKTerminate:
            raise
        except Exception as e:
            sys.stdout.write(StateMarkers.error + " Cannot merge: %s\n" % e)

    # D ---> SYMLINKS
    # 8) all are symlinks, all changed
    elif old_type == "link" and new_type == "link" and user_type == "link":
        if user_confirms(
            site,
            conflict_mode,
            "Symbolic link conflict at " + relpath,
            "'%s' is a symlink that pointed to "
            "%s in the old version and to "
            "%s in the new version. But meanwhile you "
            "changed to link target to %s. "
            "Shall I keep your link or replace it with "
            "the new default target?"
            % (relpath, os.readlink(old_path), os.readlink(new_path), os.readlink(user_path)),
            relpath,
            "keep",
            "Keep your symbolic link pointing to %s" % os.readlink(user_path),
            "replace",
            "Change link target to %s" % os.readlink(new_path),
        ):
            sys.stdout.write(StateMarkers.warn + " Keeping your   %s\n" % fn)
        else:
            os.remove(user_path)
            os.symlink(os.readlink(new_path), user_path)
            sys.stdout.write(
                StateMarkers.warn + f" Set link       {fn} to new target {os.readlink(new_path)}\n"
            )

    # E ---> FILE TYPE HAS CHANGED (NASTY)

    # Now we have to handle cases, where the file types of the three
    # versions are not identical and at the same type the user or
    # have changed the third file to. We cannot merge here, the user
    # has to decide wether to keep his version of use ours.

    # 9) We have changed the file type
    elif old_type != new_type:
        if user_confirms(
            site,
            conflict_mode,
            "File type change at " + relpath,
            "The %s %s has been changed into a %s in "
            "the new version. Meanwhile you have changed "
            "the %s of your copy of that %s. "
            "Do you want to keep your version or replace "
            "it with the new default? "
            % (old_type, relpath, new_type, user_changed_type and "type" or "content", old_type),
            relpath,
            "keep",
            "Keep your %s" % user_type,
            "replace",
            "Replace it with the new %s" % new_type,
        ):
            sys.stdout.write(StateMarkers.warn + " Keeping your version of %s\n" % fn)
        else:
            create_skeleton_file(new_skel, site.dir, relpath, replacements)
            sys.stdout.write(
                StateMarkers.warn
                + f" Replaced your {user_type} {relpath} by new default {new_type}.\n"
            )

    # 10) The user has changed the file type, we just the content
    elif old_type != user_type:
        if user_confirms(
            site,
            conflict_mode,
            "Type change conflicts with content change at " + relpath,
            "Usually %s is a %s in both the "
            "old and new version. But you have changed it "
            "into a %s. Do you want to keep that or may "
            "I replace your %s with the new default "
            "%s, please?" % (relpath, old_type, user_type, user_type, new_type),
            relpath,
            "keep",
            "Keep your %s" % user_type,
            "replace",
            "Replace it with the new %s" % new_type,
        ):
            sys.stdout.write(StateMarkers.warn + f" Keeping your {user_type} {fn}.\n")
        else:
            create_skeleton_file(new_skel, site.dir, relpath, replacements)
            sys.stdout.write(
                StateMarkers.warn
                + f" Delete your {user_type} and created new default {new_type} {fn}.\n"
            )

    # 11) This case should never happen, if I've not lost something
    else:
        if user_confirms(
            site,
            conflict_mode,
            "Something nasty happened at " + relpath,
            "You somehow fiddled along with "
            "%s, and I do not have the "
            "slightest idea what's going on here. May "
            "I please install the new default %s "
            "here, or do you want to keep your %s?" % (relpath, new_type, user_type),
            relpath,
            "keep",
            "Keep your %s" % user_type,
            "replace",
            "Replace it with the new %s" % new_type,
        ):
            sys.stdout.write(StateMarkers.warn + f" Keeping your {user_type} {fn}.\n")
        else:
            create_skeleton_file(new_skel, site.dir, relpath, replacements)
            sys.stdout.write(
                StateMarkers.warn
                + f" Delete your {user_type} and created new default {new_type} {fn}.\n"
            )

    # Now the new file/link/directory is in place, deleted or whatever. The
    # user might have interferred and changed things. We need to make sure
    # that file permissions are also updated. But the user might have changed
    # something himself.

    user_type = filetype(user_path)
    old_perm = get_skel_permissions(old_skel, old_perms, relpath)
    new_perm = get_skel_permissions(new_skel, read_skel_permissions(), relpath)
    user_perm = get_file_permissions(user_path)

    # Fix permissions not for links and only if the new type is as expected
    # and the current permissions are not as they should be
    what = permission_action(
        site=site,
        conflict_mode=conflict_mode,
        relpath=relpath,
        old_type=old_type,
        new_type=new_type,
        user_type=user_type,
        old_perm=old_perm,
        new_perm=new_perm,
        user_perm=user_perm,
    )

    if what == "keep":
        sys.stdout.write(StateMarkers.warn + f" Permissions    {user_perm:04o} {fn} (unchanged)\n")
    elif what == "default":
        try:
            os.chmod(user_path, new_perm)
            sys.stdout.write(
                StateMarkers.good + f" Permissions    {user_perm:04o} -> {new_perm:04o} {fn}\n"
            )
        except Exception as e:
            sys.stdout.write(
                StateMarkers.error
                + " Permission:    cannot change %04o -> %04o %s: %s\n"
                % (user_perm, new_perm, fn, e)
            )


def permission_action(
    *,
    site: SiteContext,
    conflict_mode: str,
    relpath: str,
    old_type: str | None,
    new_type: str | None,
    user_type: str | None,
    old_perm: int,
    new_perm: int,
    user_perm: int,
) -> str | None:
    if new_type == "link":
        return None  # Do not touch symlinks

    if user_type != new_type:
        return None  # Do not touch when type changed by the user

    if user_perm == new_perm:
        return None  # Is already in correct state

    # Special handling to prevent questions about standard situations (CMK-12090)
    if old_perm != new_perm and relpath in (
        "local/share/nagvis/htdocs/userfiles/images/maps",
        "local/share/nagvis/htdocs/userfiles/images/shapes",
        "etc/check_mk/multisite.d",
        "etc/check_mk/conf.d",
        "etc/check_mk/conf.d/wato",
        "etc/ssl/private",
        "etc/ssl/certs",
    ):
        return "default"

    # Permissions have changed in all places, but file type not
    if old_type == new_type and user_perm != old_perm and old_perm != new_perm:
        if user_confirms(
            site,
            conflict_mode,
            "Permission conflict at " + relpath,
            "The proposed permissions of %s have changed from %04o "
            "to %04o in the new version, but you have set %04o. "
            "May I use the new default permissions or do "
            "you want to keep yours?" % (relpath, old_perm, new_perm, user_perm),
            relpath,
            "keep",
            "Keep permissions at %04o" % user_perm,
            "default",
            "Set permission to %04o" % new_perm,
        ):
            return "keep"
        return "default"

    # Permissions have changed, no conflict with user
    if old_type == new_type and user_perm == old_perm:
        return "default"

    # Permissions are not correct: all other cases (where type is as expected)
    if old_perm != new_perm:
        if old_perm == user_perm:
            # The skel permissions are changed but the old skel permissions
            # are still in place. In 2.2 the permissions for other were
            # removed (Werk #15062). This results in a lot of questions for
            # the user. If the user has not adjusted the permissions from
            # the previous default, let's not ask so much questions, just
            # adjust it, a info that the permissions were adjusted will be
            # logged anyways
            return "default"

        if user_confirms(
            site,
            conflict_mode,
            "Wrong permission of " + relpath,
            "The proposed permissions of %s are %04o, but currently are "
            "%04o. May I use the new default "
            "permissions or keep yours?" % (relpath, new_perm, user_perm),
            relpath,
            "keep",
            "Keep permissions at %04o" % user_perm,
            "default",
            "Set permission to %04o" % new_perm,
        ):
            return "keep"
        return "default"

    return None


def filetype(p: str) -> str | None:
    # check for symlinks first. Might be dangling. In that
    # case os.path.exists checks the links target for existance
    # and reports it is non-existing.
    if os.path.islink(p):
        return "link"
    if not os.path.exists(p):
        return None
    if os.path.isdir(p):
        return "dir"
    return "file"


def file_contents(site: SiteContext, path: str) -> bytes:
    """Returns the file contents of a site file or a skel file"""
    if "/skel/" in path and not _is_unpatchable_file(path):
        return _instantiate_skel(site, path)

    with open(path, "rb") as f:
        return f.read()


def _instantiate_skel(site: SiteContext, path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return replace_tags(f.read(), site.replacements)
    except Exception:
        # TODO: This is a bad exception handler. Drop it
        return b""  # e.g. due to permission error


def initialize_site_ca(site: SiteContext) -> None:
    """Initialize the site local CA and create the default site certificate
    This will be used e.g. for serving SSL secured livestatus"""
    ca_path = cert_dir(Path(site.dir))
    ca = omdlib.certs.CertificateAuthority(
        root_ca=RootCA.load_or_create(root_cert_path(ca_path), CN_TEMPLATE.format(site.name)),
        ca_path=ca_path,
    )
    if not ca.site_certificate_exists(site.name):
        ca.create_site_certificate(site.name)


def agent_ca_existing(site: SiteContext) -> bool:
    return root_cert_path(cert_dir(Path(site.dir)) / "agents").exists()


def initialize_agent_ca(site: SiteContext) -> None:
    """Initialize the agents CA folder alongside a default agent signing CA.
    The default CA shall be used for issuing certificates for requesting agent controllers.
    Additional CAs/root certs that may be placed at the agent CA folder shall be used as additional
    root certs for agent receiver certificate verification (either as client or server cert)
    """
    ca_path = cert_dir(Path(site.dir)) / "agents"
    RootCA.load_or_create(root_cert_path(ca_path), f"Site '{site.name}' agent signing CA")


def link_legacy_agent_ca(site: SiteContext) -> None:
    """If there are agent controller certificates that are signed with the site CA, we have to
    maintain them (at least for a while)."""
    site_ca_path = root_cert_path(cert_dir(Path(site.dir)))
    agent_ca_dir = cert_dir(Path(site.dir)) / "agents"
    (agent_ca_dir / "legacy_ca.pem").symlink_to(site_ca_path)


def config_change(
    version_info: VersionInfo, site: SiteContext, config_hooks: ConfigHooks
) -> list[str]:
    # Check whether or not site needs to be stopped. Stop and remember to start again later
    site_was_stopped = False
    if not site.is_stopped():
        site_was_stopped = True
        stop_site(site)

    try:
        settings = read_config_change_commands()

        if not settings:
            bail_out("You need to provide config change commands via stdin: KEY=value\n")

        validate_config_change_commands(config_hooks, settings)

        changed: list[str] = []
        for key, value in settings:
            config_set_value(site, key, value, save=False)
            changed.append(key)

        save_site_conf(site)
        return changed
    finally:
        if site_was_stopped:
            start_site(version_info, site)


def read_config_change_commands() -> ConfigChangeCommands:
    settings = []
    for l in sys.stdin:
        line = l.strip()
        if not line:
            continue

        try:
            key, value = line.split("=", 1)
            settings.append((key, value))
        except ValueError:
            bail_out("Invalid config change command: %r" % line)
    return settings


def validate_config_change_commands(
    config_hooks: ConfigHooks, settings: ConfigChangeCommands
) -> None:
    # Validate the provided commands
    for key, value in settings:
        hook = config_hooks.get(key)
        if not hook:
            bail_out("Invalid config option: %r" % key)

        error_from_config_choice = _error_from_config_choice(hook.choices, value)
        if error_from_config_choice.is_error():
            bail_out(f"Invalid value for '{value} for {key}'. {error_from_config_choice.error}\n")


def config_set(site: SiteContext, config_hooks: ConfigHooks, args: Arguments) -> list[str]:
    if len(args) != 2:
        sys.stderr.write("Please specify variable name and value\n")
        config_usage()
        return []

    if not site.is_stopped():
        sys.stderr.write("Cannot change config variables while site is running.\n")
        return []

    hook_name = args[0]
    value = args[1]
    hook = config_hooks.get(hook_name)
    if not hook:
        sys.stderr.write("No such variable '%s'\n" % hook_name)
        return []

    error_from_config_choice = _error_from_config_choice(hook.choices, value)
    if error_from_config_choice.is_error():
        sys.stderr.write(f"Invalid value for '{value}'. {error_from_config_choice.error}\n")
        return []

    config_set_value(site, hook_name, value)
    return [hook_name]


def _error_from_config_choice(choices: ConfigHookChoices, value: str) -> Result[None, str]:
    # Check if value is valid. Choices are either a list of allowed keys or a
    # regular expression
    if isinstance(choices, list):
        if all(value != var for var, _descr in choices):
            return Error("Allowed are: " + ", ".join(var for var, _ in choices))
    elif isinstance(choices, re.Pattern):
        if not choices.match(value):
            return Error("Does not match allowed pattern.")
    elif isinstance(choices, ConfigChoiceHasError):
        return choices(value)
    else:
        assert_never(choices)
    return OK(None)


def config_set_all(site: SiteContext, ignored_hooks: list | None = None) -> None:
    if ignored_hooks is None:
        ignored_hooks = []

    for hook_name in sort_hooks(list(site.conf.keys())):
        # Hooks may vanish after and up- or downdate
        if not hook_exists(site, hook_name):
            continue

        if hook_name in ignored_hooks:
            continue

        _config_set(site, hook_name)


def _config_set(site: SiteContext, hook_name: str) -> None:
    value = site.conf[hook_name]

    exitcode, output = call_hook(site, hook_name, ["set", value])
    if exitcode:
        return

    if output and output != value:
        site.conf[hook_name] = output

    putenv("CONFIG_" + hook_name, site.conf[hook_name])


def config_set_value(site: SiteContext, hook_name: str, value: str, save: bool = True) -> None:
    site.conf[hook_name] = value
    _config_set(site, hook_name)

    if hook_name in ["CORE", "MKEVENTD", "PNP4NAGIOS"]:
        _update_cmk_core_config(site)

    if save:
        save_site_conf(site)


def config_usage() -> None:
    sys.stdout.write(
        """Usage of config command:

omd config               - interactive configuration menu
omd config show          - show current settings of all configuration variables
omd config show VAR      - show current setting of variable VAR
omd config set VAR VALUE - set VAR to VALUE
omd config change        - change multiple at once. Provide newline separated
                           KEY=value pairs via stdin. The site is restarted
                           automatically once in case it's currently runnig.
"""
    )


def config_show(site: SiteContext, config_hooks: ConfigHooks, args: Arguments) -> None:
    hook: ConfigHook | None
    if len(args) == 0:
        hook_names = sorted(config_hooks.keys())
        for hook_name in hook_names:
            hook = config_hooks[hook_name]
            if hook.unstructured["active"] and not hook.unstructured["deprecated"]:
                sys.stdout.write(f"{hook_name}: {site.conf[hook_name]}\n")
    else:
        output = []
        for hook_name in args:
            hook = config_hooks.get(hook_name)
            if not hook:
                sys.stderr.write("No such variable %s\n" % hook_name)
            else:
                output.append(site.conf[hook_name])

        sys.stdout.write(" ".join(output))
        sys.stdout.write("\n")


def config_configure(
    site: SiteContext, global_opts: GlobalOptions, config_hooks: ConfigHooks
) -> Iterator[str]:
    hook_names = sorted(config_hooks.keys())
    current_hook_name: str | None = ""
    menu_open = False
    current_menu = "Basic"

    # force certain order in main menu
    menu_choices = ["Basic", "Web GUI", "Addons", "Distributed Monitoring"]

    while True:
        # Rebuild hook information (values possible changed)
        menu: dict[str, list[tuple[str, str]]] = {}
        for hook_name in hook_names:
            hook = config_hooks[hook_name]
            if hook.unstructured["active"] and not hook.unstructured["deprecated"]:
                mp = hook.menu
                entries = menu.get(mp, [])
                entries.append((hook_name, site.conf[hook_name]))
                menu[mp] = entries
                if mp not in menu_choices:
                    menu_choices.append(mp)

        # Handle main menu
        if not menu_open:
            change, current_menu = dialog_menu(
                "Configuration of site %s" % site.name,
                "Interactive setting of site configuration variables. You "
                "can change values only while the site is stopped.",
                [(e, "") for e in menu_choices],
                current_menu,
                "Enter",
                "Exit",
            )
            if not change:
                return
            current_hook_name = None
            menu_open = True

        else:
            change, current_hook_name = dialog_menu(
                current_menu, "", menu[current_menu], current_hook_name, "Change", "Main menu"
            )
            if change:
                try:
                    yield from config_configure_hook(
                        site, global_opts, config_hooks, current_hook_name
                    )
                except MKTerminate:
                    raise
                except Exception as e:
                    bail_out(f"Error in hook {current_hook_name}: {e}")
            else:
                menu_open = False


def config_configure_hook(
    site: SiteContext, global_opts: GlobalOptions, config_hooks: ConfigHooks, hook_name: str
) -> Iterator[str]:
    if not site.is_stopped():
        if not dialog_yesno(
            "You cannot change configuration value while the "
            "site is running. Do you want me to stop the site now?"
        ):
            return
        stop_site(site)
        dialog_message("The site has been stopped.")

    hook = config_hooks[hook_name]
    title = hook.alias
    descr = hook.description.replace("\n\n", "\001").replace("\n", " ").replace("\001", "\n\n")
    value = site.conf[hook_name]
    choices = hook.choices

    if isinstance(choices, list):
        change, new_value = dialog_menu(title, descr, choices, value, "Change", "Cancel")
    elif isinstance(choices, re.Pattern):
        change, new_value = dialog_regex(title, descr, choices, value, "Change", "Cancel")
    elif isinstance(choices, ConfigChoiceHasError):
        change, new_value = dialog_config_choice_has_error(
            title, descr, choices, value, "Change", "Cancel"
        )
    else:
        assert_never(choices)

    if change:
        config_set_value(site, hook.name, new_value)
        save_site_conf(site)
        config_hooks = load_hook_dependencies(site, config_hooks)
        yield hook_name


def init_action(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    command: str,
    args: Arguments,
    options: CommandOptions,
) -> int:
    if site.is_disabled():
        bail_out("This site is disabled.")

    if command in ["start", "restart"]:
        prepare_and_populate_tmpfs(version_info, site)

    if len(args) > 0:
        # restrict to this daemon
        daemon: str | None = args[0]
    else:
        daemon = None

    # OMD guarantees that we are in OMD_ROOT
    with chdir(site.dir):
        if command == "status":
            return check_status(site.dir, display=True, daemon=daemon, bare="bare" in options)
        return call_init_scripts(site.dir, command, daemon)


# .
#   .--Helpers-------------------------------------------------------------.
#   |                  _   _      _                                        |
#   |                 | | | | ___| |_ __   ___ _ __ ___                    |
#   |                 | |_| |/ _ \ | '_ \ / _ \ '__/ __|                   |
#   |                 |  _  |  __/ | |_) |  __/ |  \__ \                   |
#   |                 |_| |_|\___|_| .__/ \___|_|  |___/                   |
#   |                              |_|                                     |
#   +----------------------------------------------------------------------+
#   |  Various helper functions                                            |
#   '----------------------------------------------------------------------'


def fstab_verify(site: SiteContext) -> bool:
    """Ensure that there is an fstab entry for the tmpfs of the site.
    In case there is no fstab (seen in some containers) assume everything
    is OK without fstab entry."""
    if not (fstab_path := Path("/etc", "fstab")).exists():
        return True

    mountpoint = site.tmp_dir
    with fstab_path.open() as opened_file:
        for line in opened_file:
            if "uid=%s," % site.name in line and mountpoint in line:
                return True
    bail_out(tty.error + ": fstab entry for %s does not exist" % mountpoint)


# No using os.putenv, os.getenv os.unsetenv directly because
# they seem not to work correctly in debian 503.
#
# Unsetting all vars with os.unsetenv and after that using os.getenv to read
# some vars did not bring the expected result that the environment was empty.
# The vars were still set.
#
# Same for os.putenv. Executing os.getenv right after os.putenv did not bring
# the expected result.
#
# Directly modifying os.environ seems to work.
def putenv(key: str, value: str) -> None:
    os.environ[key] = value


def getenv(key: str, default: str | None = None) -> str | None:
    if key not in os.environ:
        return default
    return os.environ[key]


def clear_environment() -> None:
    # first remove *all* current environment variables, except:
    # TERM
    # CMK_CONTAINERIZED: To better detect when running inside container (e.g. used for omd update)
    keep = ["TERM", "CMK_CONTAINERIZED"]
    for key in os.environ:
        if key not in keep:
            del os.environ[key]


def set_environment(site: SiteContext) -> None:
    putenv("OMD_SITE", site.name)
    putenv("OMD_ROOT", site.dir)
    putenv(
        "PATH",
        f"{site.dir}/local/bin:{site.dir}/bin:/usr/local/bin:/bin:/usr/bin:/sbin:/usr/sbin",
    )
    putenv("USER", site.name)

    putenv("LD_LIBRARY_PATH", f"{site.dir}/local/lib:{site.dir}/lib")
    putenv("HOME", site.dir)

    # allow user to define further environment variable in ~/etc/environment
    envfile = Path(site.dir, "etc", "environment")
    if envfile.exists():
        lineno = 0
        with envfile.open() as opened_file:
            for line in opened_file:
                lineno += 1
                line = line.strip()
                if line == "" or line[0] == "#":
                    continue  # allow empty lines and comments
                parts = line.split("=")
                if len(parts) != 2:
                    bail_out("%s: syntax error in line %d" % (envfile, lineno))
                varname = parts[0]
                value = parts[1]
                if value.startswith('"'):
                    value = value.strip('"')

                # Add the present environment when someone wants to append some
                if value.startswith("$%s:" % varname):
                    before = getenv(varname, None)
                    if before:
                        value = before + ":" + value.replace("$%s:" % varname, "")

                if value.startswith("'"):
                    value = value.strip("'")
                putenv(varname, value)

    create_config_environment(site)


def hostname() -> str:
    try:
        completed_process = subprocess.run(
            ["hostname"],
            shell=False,
            close_fds=True,
            stdout=subprocess.PIPE,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return "localhost"
    return completed_process.stdout.strip()


def replace_tags(content: bytes, replacements: Replacements) -> bytes:
    for var, value in replacements.items():
        content = content.replace(var.encode("utf-8"), value.encode("utf-8"))
    return content


def get_editor() -> str:
    editor = getenv("VISUAL", getenv("EDITOR"))
    if editor is None:
        editor = "/usr/bin/vi"

    if not os.path.exists(editor):
        editor = "vi"

    return editor


# return "| $PAGER", if a pager is available
def pipe_pager() -> str:
    pager = getenv("PAGER")
    if not pager and os.path.exists("/usr/bin/less"):
        pager = "less -F -X"
    if pager:
        return "| %s" % pager
    return ""


def call_scripts(
    site: SiteContext, phase: str, open_pty: bool, add_env: Mapping[str, str] | None = None
) -> None:
    """Calls hook scripts in defined directories."""
    path = Path(site.dir, "lib", "omd", "scripts", phase)
    if not path.exists():
        return

    env = {
        **os.environ,
        "OMD_ROOT": site.dir,
        "OMD_SITE": site.name,
        **(add_env if add_env else {}),
    }

    # NOTE: scripts have an order!
    for file in sorted(path.iterdir()):
        if file.name[0] == ".":
            continue
        _call_script(phase, open_pty, env, file)


def _call_script(  # pylint: disable=too-many-branches
    phase: str, open_pty: bool, env: Mapping[str, str], file: Path
) -> None:
    sys.stdout.write(f'Executing {phase} script "{file.name}"...')
    if open_pty:
        fd_parent, fd_child = pty.openpty()
        stdout = stderr = fd_child
    else:
        stdout = subprocess.PIPE
        stderr = subprocess.STDOUT

    with subprocess.Popen(  # nosec B602 # BNS:2b5952
        str(file),  # path-like args is not allowed when shell is true
        shell=True,
        stdout=stdout,
        stderr=stderr,
        encoding="utf-8",
        env=env,
    ) as proc:
        if open_pty:
            os.close(fd_child)
            parent: IO[str] = os.fdopen(fd_parent, buffering=1)
        else:
            assert proc.stdout is not None
            parent = proc.stdout

        wrote_output = False
        try:
            while True:
                line = parent.readline()
                if not line:
                    break
                if not wrote_output:
                    sys.stdout.write("\n")
                    wrote_output = True

                sys.stdout.write(f"-| {line}")
                sys.stdout.flush()
        except IOError:
            pass
        finally:
            if not pty:
                parent.close()

    if not proc.returncode:
        sys.stdout.write(tty.ok + "\n")
    else:
        sys.stdout.write(tty.error + " (exit code: %d)\n" % proc.returncode)
        raise SystemExit(1)


def check_site_user(site: AbstractSiteContext, site_must_exist: int) -> None:
    if not site.is_site_context():
        return

    if not site_must_exist:
        return

    if not site.exists():
        bail_out(
            "omd: The site '%s' does not exist. You need to execute "
            "omd as root or site user." % site.name
        )


# .
#   .--Commands------------------------------------------------------------.
#   |         ____                                          _              |
#   |        / ___|___  _ __ ___  _ __ ___   __ _ _ __   __| |___          |
#   |       | |   / _ \| '_ ` _ \| '_ ` _ \ / _` | '_ \ / _` / __|         |
#   |       | |__| (_) | | | | | | | | | | | (_| | | | | (_| \__ \         |
#   |        \____\___/|_| |_| |_|_| |_| |_|\__,_|_| |_|\__,_|___/         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Implementation of actual omd commands                               |
#   '----------------------------------------------------------------------'


def main_help(
    version_info: VersionInfo,
    site: AbstractSiteContext,
    global_opts: GlobalOptions | None = None,
    args: Arguments | None = None,
    options: CommandOptions | None = None,
) -> None:
    if args is None:
        args = []
    if options is None:
        options = {}
    sys.stdout.write(
        "Manage multiple monitoring sites comfortably with OMD. "
        "The Open Monitoring Distribution.\n"
    )

    if is_root():
        sys.stdout.write("Usage (called as root):\n\n")
    else:
        sys.stdout.write("Usage (called as site user):\n\n")

    for (
        command,
        only_root,
        _no_suid,
        needs_site,
        _site_must_exist,
        _confirm,
        synopsis,
        _command_function,
        _command_options,
        descr,
        _confirm_text,
    ) in COMMANDS:
        if only_root and not is_root():
            continue

        if is_root():
            if needs_site == 2:
                synopsis = "[SITE] " + synopsis
            elif needs_site == 1:
                synopsis = "SITE " + synopsis

        synopsis_width = "23" if is_root() else "16"
        sys.stdout.write((" omd %-10s %-" + synopsis_width + "s %s\n") % (command, synopsis, descr))
    sys.stdout.write(
        "\nGeneral Options:\n"
        " -V <version>                    set specific version, useful in combination with update/create\n"
        " omd COMMAND -h, --help          show available options of COMMAND\n"
    )


def main_setversion(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if len(args) == 0:
        versions = [(v, "Version %s" % v) for v in omd_versions() if not v == default_version()]

        if use_update_alternatives():
            versions = [("auto", "Auto (Update-Alternatives)")] + versions

        success, version = dialog_menu(
            "Choose new default",
            "Please choose the version to make the new default",
            versions,
            None,
            "Make default",
            "Cancel",
        )
        if not success:
            bail_out("Aborted.")
    else:
        version = args[0]

    if version != "auto" and not version_exists(version):
        bail_out("The given version does not exist.")
    if version == default_version():
        bail_out("The given version is already default.")

    # Special handling for debian based distros which use update-alternatives
    # to control the path to the omd binary, manpage and so on
    if use_update_alternatives():
        if version == "auto":
            with subprocess.Popen(["update-alternatives", "--auto", "omd"]):
                pass
        else:
            with subprocess.Popen(
                ["update-alternatives", "--set", "omd", "/omd/versions/" + version]
            ):
                pass
    else:
        if os.path.islink("/omd/versions/default"):
            os.remove("/omd/versions/default")
        os.symlink("/omd/versions/%s" % version, "/omd/versions/default")


def use_update_alternatives() -> bool:
    return os.path.exists("/var/lib/dpkg/alternatives/omd")


def main_version(
    version_info: VersionInfo,
    site: AbstractSiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if len(args) > 0:
        site = SiteContext(args[0])
        if not site.exists():
            bail_out("No such site: %s" % site.name)
        version = site.version
    else:
        version = omdlib.__version__

    if version is None:
        bail_out("Failed to determine site version")

    if "bare" in options:
        sys.stdout.write(version + "\n")
    else:
        sys.stdout.write("OMD - Open Monitoring Distribution Version %s\n" % version)


def main_versions(
    version_info: VersionInfo,
    site: AbstractSiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    for v in omd_versions():
        if v == default_version() and "bare" not in options:
            sys.stdout.write("%s (default)\n" % v)
        else:
            sys.stdout.write("%s\n" % v)


def default_version() -> str:
    return os.path.basename(
        os.path.realpath(os.path.join(omdlib.utils.omd_base_path(), "omd/versions/default"))
    )


def omd_versions() -> Iterable[str]:
    try:
        return sorted(
            [
                v
                for v in os.listdir(os.path.join(omdlib.utils.omd_base_path(), "omd/versions"))
                if v != "default"
            ]
        )
    except FileNotFoundError:
        return []


def version_exists(v: str) -> bool:
    return v in omd_versions()


def main_sites(
    version_info: VersionInfo,
    site: AbstractSiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if sys.stdout.isatty() and "bare" not in options:
        sys.stdout.write("SITE             VERSION          COMMENTS\n")
    for sitename in all_sites():
        site = SiteContext(sitename)
        tags = []
        if "bare" in options:
            sys.stdout.write("%s\n" % site.name)
        else:
            disabled = site.is_disabled()
            v = site.version
            if v is None:
                v = "(none)"
                tags.append("empty site dir")
            elif v == default_version():
                tags.append("default version")
            if disabled:
                tags.append(tty.bold + tty.red + "disabled" + tty.normal)
            sys.stdout.write("%-16s %-16s %s " % (site.name, v, ", ".join(tags)))
            sys.stdout.write("\n")


# Bail out if name for new site is not valid (needed by create/mv/cp)
def sitename_must_be_valid(site, reuse=False):
    # type (SiteContext, bool) -> None
    # Make sanity checks before starting any action

    if not re.match("^[a-zA-Z_][a-zA-Z_0-9]{0,15}$", site.name):
        bail_out(
            "Invalid site name. Must begin with a character, may contain characters, digits and _ and have length 1 up to 16"
        )

    if not reuse and site.exists():
        bail_out("Site '%s' already existing." % site.name)
    if not reuse and group_exists(site.name):
        bail_out("Group '%s' already existing." % site.name)
    if not reuse and user_exists(site.name):
        bail_out("User '%s' already existing." % site.name)


def main_create(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    reuse = False
    if "reuse" in options:
        reuse = True
        if not user_verify(version_info, site):
            bail_out("Error verifying site user.")

    sitename_must_be_valid(site, reuse)

    # Create operating system user for site
    uid = options.get("uid")
    gid = options.get("gid")
    if not reuse:
        useradd(version_info, site, uid, gid)

    if reuse:
        fstab_verify(site)
    else:
        create_site_dir(site)
        add_to_fstab(site, tmpfs_size=options.get("tmpfs-size"))

    config_settings: Config = {}
    if "no-autostart" in options:
        config_settings["AUTOSTART"] = "off"
        sys.stdout.write("Going to set AUTOSTART to off.\n")

    if "no-tmpfs" in options:
        config_settings["TMPFS"] = "off"
        sys.stdout.write("Going to set TMPFS to off.\n")

    if "no-init" not in options:
        admin_password = init_site(version_info, site, global_opts, config_settings, options)
        welcome_message(site, admin_password)

    else:
        sys.stdout.write(
            f"Create new site {site.name} in disabled state and with empty {site.dir}.\n"
        )
        sys.stdout.write("You can now mount a filesystem to %s.\n" % (site.dir))
        sys.stdout.write("Afterwards you can initialize the site with 'omd init'.\n")


def welcome_message(site: SiteContext, admin_password: Password) -> None:
    sys.stdout.write(f"Created new site {site.name} with version {omdlib.__version__}.\n\n")
    sys.stdout.write(
        f"  The site can be started with {tty.bold}omd start {site.name}{tty.normal}.\n"
    )
    sys.stdout.write(
        "  The default web UI is available at %shttp://%s/%s/%s\n"
        % (tty.bold, hostname(), site.name, tty.normal)
    )
    sys.stdout.write("\n")
    sys.stdout.write(
        "  The admin user for the web applications is %scmkadmin%s with password: %s%s%s\n"
        % (tty.bold, tty.normal, tty.bold, admin_password.raw, tty.normal)
    )
    sys.stdout.write(
        "  For command line administration of the site, log in with %s'omd su %s'%s.\n"
        % (tty.bold, site.name, tty.normal)
    )
    sys.stdout.write(
        "  After logging in, you can change the password for cmkadmin with "
        "%s'cmk-passwd cmkadmin'%s.\n" % (tty.bold, tty.normal)
    )
    sys.stdout.write("\n")


def main_init(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if not site.is_disabled():
        bail_out(
            "Cannot initialize site that is not disabled.\n"
            "Please call 'omd disable %s' first." % site.name
        )

    is_verbose = logger.isEnabledFor(VERBOSE)

    if not site.is_empty():
        if not global_opts.force:
            bail_out(
                "The site's home directory is not empty. Please add use\n"
                "'omd --force init %s' if you want to erase all data." % site.name
            )

        # We must not delete the directory itself, just its contents.
        # The directory might be a separate filesystem. This is not quite
        # unlikely, since people using 'omd init' are doing this most times
        # because they are working with clusters and separate filesystems for
        # each site.
        sys.stdout.write("Wiping the contents of %s..." % site.dir)
        for entry in os.listdir(site.dir):
            if entry not in [".", ".."]:
                path = site.dir + "/" + entry
                if is_verbose:
                    sys.stdout.write("\n   deleting %s..." % path)
                if os.path.islink(path) or not os.path.isdir(path):
                    os.remove(path)
                else:
                    shutil.rmtree(site.dir + "/" + entry)
        ok()

    # Do the things that have been ommited on omd create --disabled
    admin_password = init_site(version_info, site, global_opts, config_settings={}, options=options)
    welcome_message(site, admin_password)


def init_site(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    config_settings: Config,
    options: CommandOptions,
) -> Password:
    apache_reload = "apache-reload" in options

    # Create symbolic link to version
    create_version_symlink(site, omdlib.__version__)

    # Build up directory structure with symbolic links relative to
    # the version link we just create
    for d in ["bin", "include", "lib", "share"]:
        os.symlink("version/" + d, site.dir + "/" + d)

    # Create skeleton files of non-tmp directories
    create_skeleton_files(site, ".")

    # Save the skeleton files used to initialize this site
    save_version_meta_data(site, omdlib.__version__)

    # Set the initial password of the default admin user
    admin_password = calculate_admin_password(options)
    set_admin_password(site, admin_password)

    # Change ownership of all files and dirs to site user
    chown_tree(site.dir, site.name)

    site.load_config(load_defaults(site))  # load default values from all hooks
    if config_settings:  # add specific settings
        for hook_name, value in config_settings.items():
            site.conf[hook_name] = value
    create_config_environment(site)

    # Change the few files that config save as created as root
    chown_tree(site.dir, site.name)

    finalize_site(version_info, site, CommandType.create, apache_reload)

    return admin_password


# Is being called at the end of create, cp and mv.
# What is "create", "mv" or "cp". It is used for
# running the appropriate hooks.
def finalize_site(
    version_info: VersionInfo, site: SiteContext, command_type: CommandType, apache_reload: bool
) -> None:
    # Now we need to do a few things as site user. Note:
    # - We cannot use setuid() here, since we need to get back to root.
    # - We cannot use seteuid() here, since the id command call will then still
    #   report root and confuse some tools
    # - We cannot sue setresuid() here, since that is not supported an Python 2.4
    # So we need to fork() and use a real setuid() here and leave the main process
    # at being root.
    pid = os.fork()
    if pid == 0:
        try:
            # From now on we run as normal site user!
            switch_to_site_user(site)

            # avoid executing hook 'TMPFS' and cleaning an initialized tmp directory
            # see CMK-3067
            finalize_site_as_user(version_info, site, command_type, ignored_hooks=["TMPFS"])
            sys.exit(0)
        except Exception as e:
            bail_out("Failed to finalize site: %s" % e)
    else:
        _wpid, status = os.waitpid(pid, 0)
        if status:
            bail_out("Error in non-priviledged sub-process.")

    # The config changes above, made with the site user, have to be also available for
    # the root user, so load the site config again. Otherwise e.g. changed
    # APACHE_TCP_PORT would not be recognized
    site.load_config(load_defaults(site))
    register_with_system_apache(version_info, site, apache_reload)


def finalize_site_as_user(
    version_info: VersionInfo,
    site: SiteContext,
    command_type: CommandType,
    ignored_hooks: list[str] | None = None,
) -> None:
    # Mount and create contents of tmpfs. This must be done as normal
    # user. We also could do this at 'omd start', but this might confuse
    # users. They could create files below tmp which would be shadowed
    # by the mount.
    prepare_and_populate_tmpfs(version_info, site)

    # Run all hooks in order to setup things according to the
    # configuration settings
    config_set_all(site, ignored_hooks)
    _update_cmk_core_config(site)
    initialize_site_ca(site)
    initialize_agent_ca(site)
    save_site_conf(site)

    if command_type in [CommandType.create, CommandType.copy, CommandType.restore_as_new_site]:
        save_instance_id(file_path=get_instance_id_file_path(Path(site.dir)), instance_id=uuid4())

    call_scripts(site, "post-" + command_type.short, open_pty=sys.stdout.isatty())


def main_rm(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    # omd rm is called as root but the init scripts need to be called as
    # site user but later steps need root privilegies. So a simple user
    # switch to the site user would not work. Better create a subprocess
    # for this dedicated action and switch to the user in that subprocess
    with subprocess.Popen(["omd", "stop", site.name]):
        pass

    reuse = "reuse" in options
    kill = "kill" in options

    if user_logged_in(site.name):
        if not kill:
            bail_out("User '%s' still logged in or running processes." % site.name)
        else:
            kill_site_user_processes(site, global_opts, exclude_current_and_parents=True)

    if tmpfs_mounted(site.name):
        unmount_tmpfs(site, kill=kill)

    # Remove include-hook for Apache and tell apache
    # Needs to be cleaned up before removing the site directory. Otherwise a
    # parallel restart / reload of the apache may fail, because the apache hook
    # refers to a not existing site apache config.
    unregister_from_system_apache(version_info, site, apache_reload="apache-reload" in options)

    if not reuse:
        remove_from_fstab(site)
        sys.stdout.write("Deleting user and group %s..." % site.name)
        os.chdir("/")  # Site directory not longer existant after userdel
        userdel(site.name)
        ok()

    if os.path.exists(site.dir):  # should be done by userdel
        sys.stdout.write("Deleting all data (%s)..." % site.dir)
        shutil.rmtree(site.dir)
        ok()

    if reuse:
        create_site_dir(site)
        os.mkdir(site.tmp_dir)
        os.chown(site.tmp_dir, user_id(site.name), group_id(site.name))


def create_site_dir(site: SiteContext) -> None:
    try:
        os.makedirs(site.dir)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    os.chown(site.dir, user_id(site.name), group_id(site.name))
    # If the site-dir is not world executable files in the site are all not readable/writeable
    os.chmod(site.dir, 0o751)  # nosec B103 # BNS:7e6b08


def main_disable(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if site.is_disabled():
        sys.stderr.write("This site is already disabled.\n")
        sys.exit(0)

    stop_if_not_stopped(site)
    unmount_tmpfs(site, kill="kill" in options)
    sys.stdout.write("Disabling Apache configuration for this site...")
    unregister_from_system_apache(version_info, site, apache_reload=False)


def main_enable(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if not site.is_disabled():
        sys.stderr.write("This site is already enabled.\n")
        sys.exit(0)
    sys.stdout.write("Re-enabling Apache configuration for this site...")
    register_with_system_apache(version_info, site, apache_reload=False)


def main_update_apache_config(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    site.load_config(load_defaults(site))
    if _is_apache_enabled(site):
        register_with_system_apache(version_info, site, apache_reload=True)
    else:
        unregister_from_system_apache(version_info, site, apache_reload=True)


def _is_apache_enabled(site: SiteContext) -> bool:
    return site.conf["APACHE_MODE"] != "none"


def _get_conflict_mode(options: CommandOptions) -> str:
    conflict_mode = cast(str, options.get("conflict", "ask"))

    if conflict_mode not in ["ask", "install", "keepold", "abort"]:
        bail_out("Argument to --conflict must be one of ask, install, keepold and abort.")

    return conflict_mode


def main_mv_or_cp(  # pylint: disable=too-many-branches
    version_info: VersionInfo,
    old_site: SiteContext,
    global_opts: GlobalOptions,
    command_type: CommandType,
    args: Arguments,
    options: CommandOptions,
) -> None:
    conflict_mode = _get_conflict_mode(options)
    action = "rename" if command_type is CommandType.move else "copy"

    if len(args) != 1:
        bail_out("omd: Usage: omd %s oldname newname" % command_type.short)
    new_site = SiteContext(args[0])

    reuse = False
    if "reuse" in options:
        reuse = True
        if not user_verify(version_info, new_site):
            bail_out("Error verifying site user.")
        fstab_verify(new_site)

    sitename_must_be_valid(new_site, reuse)

    if not old_site.is_stopped():
        bail_out(f"Cannot {action} site '{old_site.name}' while it is running.")

    pids = find_processes_of_user(old_site.name)
    if pids:
        bail_out(
            "Cannot %s site '%s' while there are processes owned by %s.\n"
            "PIDs: %s" % (action, old_site.name, old_site.name, " ".join(pids))
        )

    if command_type is CommandType.move:
        unmount_tmpfs(old_site, kill="kill" in options)
        if not reuse:
            remove_from_fstab(old_site)

    sys.stdout.write(
        "{}ing site {} to {}...".format(
            command_type is CommandType.move and "Mov" or "Copy", old_site.name, new_site.name
        )
    )
    sys.stdout.flush()

    # Create new user. Note: even on mv we need to create a new user.
    # Linux does not (officially) allow to rename a user.
    uid = options.get("uid")
    gid = options.get("gid")
    if not reuse:
        useradd(version_info, new_site, uid, gid)  # None for uid/gid means: let Linux decide

    if command_type is CommandType.move and not reuse:
        # Rename base directory and apache config
        os.rename(old_site.dir, new_site.dir)
        delete_apache_hook(old_site.name)
    else:
        # Make exact file-per-file copy with same user but already new name
        if not reuse:
            os.mkdir(new_site.dir)

        addopts = []
        for p in omdlib.backup.get_exclude_patterns(options):
            addopts += ["--exclude", "/%s" % p]

        if global_opts.verbose:
            addopts += ["-v"]

        with subprocess.Popen(
            ["rsync", "-arx"] + addopts + [old_site.dir + "/", new_site.dir + "/"]
        ):
            pass

        httpdlogdir = new_site.dir + "/var/log/apache"
        if not os.path.exists(httpdlogdir):
            os.mkdir(httpdlogdir)

        rrdcacheddir = new_site.dir + "/var/rrdcached"
        if not os.path.exists(rrdcacheddir):
            os.mkdir(rrdcacheddir)

    # give new user all files
    chown_tree(new_site.dir, new_site.name)

    # Change config files from old to new site (see rename_site())
    patch_skeleton_files(conflict_mode, old_site, new_site)

    # In case of mv now delete old user
    if command_type is CommandType.move and not reuse:
        userdel(old_site.name)

    # clean up old site
    if command_type is CommandType.move and reuse:
        main_rm(version_info, old_site, global_opts, [], {"reuse": None})

    sys.stdout.write("OK\n")

    # Now switch over to the new site as currently active site
    new_site.load_config(load_defaults(new_site))
    set_environment(new_site)

    # Entry for tmps in /etc/fstab
    if not reuse:
        add_to_fstab(new_site, tmpfs_size=options.get("tmpfs-size"))

    # Needed by the post-rename-site script
    putenv("OLD_OMD_SITE", old_site.name)

    finalize_site(version_info, new_site, command_type, "apache-reload" in options)


def main_diff(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    from_version = site.version
    if from_version is None:
        bail_out("Failed to determine site version")
    from_skelroot = site.version_skel_dir

    # If arguments are added and those arguments are directories,
    # then we just output the general state of the file. If only
    # one file is specified, we directly show the unified diff.
    # This behaviour can also be forced by the OMD option -v.

    if len(args) == 0:
        args = ["."]
    elif len(args) == 1 and os.path.isfile(args[0]):
        global_opts = GlobalOptions(
            verbose=True,
            force=global_opts.force,
            interactive=global_opts.interactive,
            orig_working_directory=global_opts.orig_working_directory,
        )

    for arg in args:
        diff_list(global_opts, options, site, from_skelroot, from_version, arg)


def diff_list(
    global_opts: GlobalOptions,
    options: CommandOptions,
    site: SiteContext,
    from_skelroot: str,
    from_version: str,
    orig_path: str,
) -> None:
    # Compare a list of files/directories with the original state and output differences. In verbose
    # mode, we output the complete diff, otherwise just the state. Only files present in skel/ are
    # handled at all.

    old_perms = site.skel_permissions

    # Prepare paths:
    # orig_path: this was specified by the user
    # rel_path:  path relative to the site's dir
    # abs_path:  absolute path

    # Get absolute path to site dir. This can be (/opt/omd/sites/XXX)
    # due to the symbolic link /omd
    old_dir = os.getcwd()
    os.chdir(site.dir)
    abs_sitedir = os.getcwd()
    os.chdir(old_dir)

    # Create absolute paths first
    abs_path = orig_path
    if not abs_path.startswith("/"):
        if abs_path == ".":
            abs_path = ""
        elif abs_path.startswith("./"):
            abs_path = abs_path[2:]
        abs_path = os.getcwd() + "/" + abs_path
    abs_path = abs_path.rstrip("/")

    # Make sure that path does not lie outside the OMD site
    if abs_path.startswith(site.dir):
        rel_path = abs_path[len(site.dir) + 1 :]
    elif abs_path.startswith(abs_sitedir):
        rel_path = abs_path[len(abs_sitedir) + 1 :]
    else:
        bail_out("Sorry, 'omd diff' only works for files in the site's directory.")

    if not os.path.isdir(abs_path):
        print_diff(
            rel_path, global_opts, options, site, from_skelroot, site.dir, from_version, old_perms
        )
    else:
        if not rel_path:
            rel_path = "."

        for file_path in walk_skel(
            from_skelroot, conflict_mode="ask", depth_first=False, relbase=rel_path
        ):
            print_diff(
                file_path,
                global_opts,
                options,
                site,
                from_skelroot,
                site.dir,
                from_version,
                old_perms,
            )


def print_diff(
    rel_path: str,
    global_opts: GlobalOptions,
    options: CommandOptions,
    site: SiteContext,
    source_path: str,
    target_path: str,
    source_version: str,
    source_perms: Permissions,
) -> None:
    source_file = source_path + "/" + rel_path
    target_file = target_path + "/" + rel_path

    source_perm = get_skel_permissions(source_path, source_perms, rel_path)
    target_perm = get_file_permissions(target_file)

    source_type = filetype(source_file)
    target_type = filetype(target_file)

    changed_type, changed_content, changed = file_status(site, source_file, target_file)

    if not changed:
        return

    fn = tty.bold + tty.bgblue + tty.white + rel_path + tty.normal
    fn = tty.bold + rel_path + tty.normal

    def print_status(color: str, f: str, status: str, long_out: str) -> None:
        if "bare" in options:
            sys.stdout.write(f"{status} {f}\n")
        elif not global_opts.verbose:
            sys.stdout.write(color + f" {long_out} {f}\n")
        else:
            arrow = tty.magenta + "->" + tty.normal
            if "c" in status:
                source_content = file_contents(site, source_file)
                if os.system("which colordiff > /dev/null 2>&1") == 0:  # nosec B605 # BNS:2b5952
                    diff = "colordiff"
                else:
                    diff = "diff"
                subprocess.run(
                    [diff, "-", target_file],
                    close_fds=True,
                    shell=False,
                    input=source_content,
                    check=False,
                )
            elif status == "p":
                sys.stdout.write(f"    {source_perm} {arrow} {target_perm}\n")
            elif "t" in status:
                sys.stdout.write(f"    {source_type} {arrow} {target_type}\n")

    if not target_type:
        print_status(StateMarkers.good, fn, "r", "Removed")
        return

    if changed_type and changed_content:
        print_status(StateMarkers.good, fn, "tc", "Changed type and content")

    elif changed_type and not changed_content:
        print_status(StateMarkers.good, fn, "t", "Changed type")

    elif changed_content and not changed_type:
        print_status(StateMarkers.good, fn, "c", "Changed content")

    if source_perm != target_perm:
        print_status(StateMarkers.warn, fn, "p", "Changed permissions")


def main_update(  # pylint: disable=too-many-branches
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    conflict_mode = _get_conflict_mode(options)

    if not site.is_stopped():
        bail_out("Please completely stop '%s' before updating it." % site.name)

    # Unmount tmp. We need to recreate the files and directories
    # from the new version after updating.
    unmount_tmpfs(site)

    # Source version: the version of the site we deal with
    from_version = site.version
    if from_version is None:
        bail_out("Failed to determine site version")

    # Target version: the version of the OMD binary
    to_version = omdlib.__version__

    # source and target are identical if 'omd update' is called
    # from within a site. In that case we make the user choose
    # the target version explicitely and the re-exec the bin/omd
    # of the target version he has choosen.
    if from_version == to_version:
        possible_versions = [v for v in omd_versions() if v != from_version]
        possible_versions.sort(reverse=True)
        if len(possible_versions) == 0:
            bail_out("There is no other OMD version to update to.")
        elif len(possible_versions) == 1:
            to_version = possible_versions[0]
        else:
            success, to_version = dialog_menu(
                "Choose target version",
                "Please choose the version this site should be updated to",
                [(v, "Version %s" % v) for v in possible_versions],
                possible_versions[0],
                "Update now",
                "Cancel",
            )
            if not success:
                bail_out("Aborted.")
        exec_other_omd(site, to_version, "update")

    if (
        isinstance(
            compatibility := versions_compatible(
                _omd_to_check_mk_version(from_version), _omd_to_check_mk_version(to_version)
            ),
            VersionsIncompatible,
        )
        and not global_opts.force
    ):
        bail_out(
            f"ERROR: You are trying to update from {from_version} to {to_version} which is not "
            f"supported. Reason: {compatibility}\n\n"
            "* Major downgrades are not supported\n"
            "* Major version updates need to be done step by step.\n\n"
            "If you are really sure about what you are doing, you can still do the "
            "update with '-f'.\n"
            "But you will be on your own from there."
        )

    # This line is reached, if the version of the OMD binary (the target)
    # is different from the current version of the site.
    if not global_opts.force and not dialog_yesno(
        "You are going to update the site %s from version %s to version %s. "
        "This will include updating all of your configuration files and merging "
        "changes in the default files with changes made by you. In case of conflicts "
        "your help will be needed." % (site.name, from_version, to_version),
        "Update!",
        "Abort",
    ):
        bail_out("Aborted.")

    # In case the user changes the installed Checkmk Edition during update let the
    # user confirm this step.
    from_edition, to_edition = _get_edition(from_version), _get_edition(to_version)
    if from_edition == "managed" and to_edition != "managed" and not global_opts.force:
        bail_out(f"ERROR: Updating from {from_edition} to {to_edition} is not possible. Aborted.")

    if (
        from_edition != to_edition
        and not global_opts.force
        and not dialog_yesno(
            text="You are updating from %s Edition to %s Edition. Is this intended?"
            % (from_edition.title(), to_edition.title()),
            default_no=True,
        )
    ):
        bail_out("Aborted.")

    # - 2.1 and before were compatible with the old and new hook configuration
    # - Checkmk 2.2 enforces the new hook with this condition
    # TODO: Remove with 2.3
    if not global_opts.force and has_old_apache_hook_in_site(site):
        bail_out(
            "ERROR: You have to update the system apache configuration in order to proceed "
            "with this update.\n\n"
            "Previous Checkmk versions were compatible with the old configuration, but this "
            "version requires\n"
            f"you to execute 'omd update-apache-config {site.name}' as root user.\n\n"
            "Have a look at #14281 for further information."
        )

    try:
        hook_up_to_date = is_apache_hook_up_to_date(site)
    except PermissionError:
        # In case the hook can not be read, assume the hook needs to be updated
        hook_up_to_date = False

    if (
        not hook_up_to_date
        and not global_opts.force
        and not dialog_yesno(
            "This update requires additional actions: The system apache configuration has changed "
            "with the new version and needs to be updated.\n\n"
            f"You will have to execute 'omd update-apache-config {site.name}' as root user.\n\n"
            "Please do it right after 'omd update' to prevent inconsistencies. Have a look at "
            "#14281 for further information.\n\n"
            "Do you want to proceed?"
        )
    ):
        bail_out("Aborted.")

    is_tty = sys.stdout.isatty()
    start_logging(site.dir + "/var/log/update.log")

    sys.stdout.write(
        "%s - Updating site '%s' from version %s to %s...\n\n"
        % (time.strftime("%Y-%m-%d %H:%M:%S"), site.name, from_version, to_version)
    )

    # etc/icinga/icinga.d/pnp4nagios.cfg was created by the PNP4NAGIOS OMD hook in previous
    # versions. Since we have removed Icinga 1 the "omd update" command tries to remove the
    # directory and complains about a non empty directory because of this left over symlink.
    # The hook could clean it up on it's own, but it would be too late and the warning is
    # displayed. We want to reduce the confusions about this, so we remove this file in
    # advance here.
    # This may be cleaned up one day, e.g. with 1.8 or 1.9. The worst that
    # would happen is that the users will be asked what to do.
    if os.path.lexists(site.dir + "/etc/icinga/icinga.d/pnp4nagios.cfg"):
        os.unlink(site.dir + "/etc/icinga/icinga.d/pnp4nagios.cfg")

    # Now apply changes of skeleton files. This can be done
    # in two ways:
    # 1. creating a patch from the old default files to the new
    #    default files and applying that to the current files
    # 2. creating a patch from the old default files to the current
    #    files and applying that to the new default files
    # We implement the first method.

    # In case the version_meta is stored in the site and it's the data of the
    # old version we are facing, use these files instead of the files from the
    # version directory. This makes updates possible without the old version.
    old_perms = site.skel_permissions

    from_skelroot = site.version_skel_dir
    to_skelroot = "/omd/versions/%s/skel" % to_version

    # First walk through skeleton files of new version
    for relpath in walk_skel(to_skelroot, conflict_mode=conflict_mode, depth_first=False):
        _execute_update_file(
            relpath, site, conflict_mode, from_version, to_version, to_edition, old_perms
        )

    # Now handle files present in old but not in new skel files
    for relpath in walk_skel(
        from_skelroot, conflict_mode=conflict_mode, depth_first=True, exclude_if_in=to_skelroot
    ):
        _execute_update_file(
            relpath, site, conflict_mode, from_version, to_version, to_edition, old_perms
        )

    # Change symbolic link pointing to new version
    create_version_symlink(site, to_version)
    save_version_meta_data(site, to_version)

    # Prepare for config_set_all: Refresh the site configuration, because new hooks may introduce
    # new settings and default values.
    site.load_config(load_defaults(site))

    # Execute some builtin initializations before executing the update-pre-hooks
    initialize_livestatus_tcp_tls_after_update(site)
    initialize_site_ca(site)

    preexisting = agent_ca_existing(site)
    initialize_agent_ca(site)
    if not preexisting:
        link_legacy_agent_ca(site)

    # Let hooks of the new(!) version do their work and update configuration.
    config_set_all(site)

    # Before the hooks can be executed the tmpfs needs to be mounted. This requires access to the
    # initialized tmpfs.
    prepare_and_populate_tmpfs(version_info, site)

    call_scripts(
        site,
        "update-pre-hooks",
        open_pty=is_tty,
        add_env={
            "OMD_CONFLICT_MODE": conflict_mode,
            "OMD_TO_EDITION": to_edition,
            "OMD_FROM_VERSION": from_version,
            "OMD_TO_VERSION": to_version,
            "OMD_FROM_EDITION": from_edition,
        },
    )

    # We previously executed "cmk -U" multiple times in the hooks CORE, MKEVENTD, PNP4NAGIOS to
    # update the core configuration. To only execute it once, we do it here.
    #
    # Please note that this is explicitly done AFTER update-pre-hooks, because that executes
    # "cmk-update-config" which updates e.g. the autochecks from previous versions to make it
    # loadable by the code of the NEW version
    _update_cmk_core_config(site)

    save_site_conf(site)

    call_scripts(site, "post-update", open_pty=is_tty)

    if from_edition != "cloud" and to_edition == "cloud":
        sys.stdout.write(
            f"{tty.bold}You are now starting your trial of Checkmk Cloud Edition. If you are "
            f"intending to use Checkmk to monitor more than 750 services after 30 days, you must "
            f"purchase a license. In case you already have a license, please enter your license "
            f"credentials on the product's licensing page "
            f"(Setup > Maintenance > Licensing > Edit settings).{tty.normal}\n"
        )

    sys.stdout.write("Finished update.\n\n")
    stop_logging()


def _update_cmk_core_config(site: SiteContext) -> None:
    if site.conf["CORE"] == "none":
        return  # No core config is needed in this case

    sys.stdout.write("Updating core configuration...\n")
    try:
        subprocess.check_call(["cmk", "-U"], shell=False)
    except subprocess.SubprocessError:
        bail_out("Could not update core configuration. Aborting.")


def initialize_livestatus_tcp_tls_after_update(site: SiteContext) -> None:
    """Keep unencrypted livestatus for old sites

    In case LIVESTATUS_TCP is on prior to the update, don't enable the
    encryption for compatibility. Only enable it for new sites (by the
    default setting)."""
    if site.conf["LIVESTATUS_TCP"] != "on":
        return  # Livestatus TCP not enabled, no need to set this option

    if "LIVESTATUS_TCP_TLS" in site.read_site_config():
        return  # Is already set in this site

    config_set_value(site, "LIVESTATUS_TCP_TLS", value="off", save=True)


def _create_livestatus_tcp_socket_link(site: SiteContext) -> None:
    """Point the xinetd to the livestatus socket inteded by LIVESTATUS_TCP_TLS"""
    link_path = site.tmp_dir + "/run/live-tcp"
    target = "live-tls" if site.conf["LIVESTATUS_TCP_TLS"] == "on" else "live"

    if os.path.lexists(link_path):
        os.unlink(link_path)

    parent_dir = os.path.dirname(link_path)
    if not os.path.exists(parent_dir):
        os.makedirs(parent_dir)

    os.symlink(target, link_path)


def _get_edition(
    omd_version: str,
) -> Literal["raw", "enterprise", "managed", "free", "cloud", "saas", "unknown"]:
    """Returns the long Checkmk Edition name or "unknown" of the given OMD version"""
    parts = omd_version.split(".")
    if parts[-1] == "demo":
        edition_short = parts[-2]
    else:
        edition_short = parts[-1]

    if edition_short == "cre":
        return "raw"
    if edition_short == "cee":
        return "enterprise"
    if edition_short == "cme":
        return "managed"
    if edition_short == "cfe":
        return "free"
    if edition_short == "cce":
        return "cloud"
    if edition_short == "cse":
        return "saas"
    return "unknown"


def _get_raw_version(omd_version: str) -> str:
    return omd_version[:-4]


def _omd_to_check_mk_version(omd_version: str) -> Version:
    """
    >>> f = _omd_to_check_mk_version
    >>> f("2.0.0p3.cee")
    Version(_BaseVersion(major=2, minor=0, sub=0), _Release(r_type=RType.p, value=3))
    >>> f("1.6.0p3.cee.demo")
    Version(_BaseVersion(major=1, minor=6, sub=0), _Release(r_type=RType.p, value=3))
    >>> f("2.0.0p3.cee")
    Version(_BaseVersion(major=2, minor=0, sub=0), _Release(r_type=RType.p, value=3))
    >>> f("2021.12.13.cee")
    Version(None, _Release(r_type=RType.daily, value=_BuildDate(year=2021, month=12, day=13)))
    """
    parts = omd_version.split(".")

    # Before we had the free edition, we had versions like ".cee.demo". Since we deal with old
    # versions, we need to care about this.
    if parts[-1] == "demo":
        del parts[-1]

    # Strip the edition suffix away
    del parts[-1]

    return Version.from_str(".".join(parts))


def main_umount(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    only_version = options.get("version")

    # if no site is selected, all sites are affected
    exit_status = 0
    if not site.is_site_context():
        for site_id in all_sites():
            # Set global vars for the current site
            site = SiteContext(site_id)

            if only_version and site.version != only_version:
                continue

            # Skip the site even when it is partly running
            if not site.is_stopped():
                sys.stderr.write(
                    "Cannot unmount tmpfs of site '%s' while it is running.\n" % site.name
                )
                continue

            sys.stdout.write(f"{tty.bold}Unmounting tmpfs of site {site.name}{tty.normal}...")
            sys.stdout.flush()

            if not show_success(unmount_tmpfs(site, False, kill="kill" in options)):
                exit_status = 1
    else:
        # Skip the site even when it is partly running
        if not site.is_stopped():
            bail_out("Cannot unmount tmpfs of site '%s' while it is running." % site.name)
        unmount_tmpfs(site, kill="kill" in options)
    sys.exit(exit_status)


def main_init_action(  # pylint: disable=too-many-branches
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    command: str,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if site.is_site_context():
        exit_status = init_action(version_info, site, global_opts, command, args, options)

        # When the whole site is about to be stopped check for remaining
        # processes and terminate them
        if command == "stop" and not args and exit_status == 0:
            terminate_site_user_processes(site, global_opts)
            # Even if we are not explicitly executing an unmount of the tmpfs, this may be the
            # "stop" before shutting down the computer. Create a tmpfs dump now, just to be sure.
            save_tmpfs_dump(site)

        if command == "start":
            if not (instance_id_file_path := get_instance_id_file_path(Path(site.dir))).exists():
                # Existing sites may not have an instance ID yet. After an update we create a new one.
                save_instance_id(file_path=instance_id_file_path, instance_id=uuid4())
            _update_license_usage(site)

        sys.exit(exit_status)

    # if no site is selected, all sites are affected

    only_version = options.get("version")
    bare = "bare" in options
    parallel = "parallel" in options

    max_site_len = max([8] + [len(site_id) for site_id in all_sites()])

    def parallel_output(site_id: str, line: str) -> None:
        sys.stdout.write(("%-" + str(max_site_len) + "s - %s") % (site_id, line))

    exit_states, processes = [], []
    for sitename in all_sites():
        site = SiteContext(sitename)

        if site.version is None:  # skip partially created sites
            continue

        if only_version and site.version != only_version:
            continue

        # Skip disabled sites completely
        if site.is_disabled():
            continue

        site.load_config(load_defaults(site))

        # Handle non autostart sites
        if command in ["start", "restart", "reload"] or ("auto" in options and command == "status"):
            if not global_opts.force and not site.is_autostart():
                if bare:
                    continue

                if not parallel:
                    sys.stdout.write("Ignoring site '%s': AUTOSTART != on\n" % site.name)
                else:
                    parallel_output(site.name, "Ignoring since autostart is disabled\n")

                continue

        if command == "status" and bare:
            sys.stdout.write("[%s]\n" % site.name)
        elif not parallel:
            sys.stdout.write(f"{tty.bold}Doing '{command}' on site {site.name}:{tty.normal}\n")
        else:
            parallel_output(site.name, "Invoking '%s'\n" % (command))
        sys.stdout.flush()

        # We need to open a subprocess, because each site must be started with the account of the
        # site user. And after setuid() we cannot return.
        stdout: int | IO[str] = sys.stdout if not parallel else subprocess.PIPE
        stderr: int | IO[str] = sys.stderr if not parallel else subprocess.STDOUT
        bare_arg = ["--bare"] if bare else []
        p = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.argv[0], command] + bare_arg + [site.name] + args,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            encoding="utf-8",
        )

        if parallel:
            if p.stdout is not None:
                # Make the output non blocking
                fd = p.stdout.fileno()
                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

            processes.append((site.name, p))
        else:
            exit_states.append(p.wait())
            if not bare:
                sys.stdout.write("\n")

    # In parallel mode wait for completion of all processes and collect
    # the output produced on stdout in the meantime. Since the processes
    # work in parallel and we want to have nearly "live" output, we process
    # the output line by line and prefix each line with the ID of the site.
    # The output of a single process must not block the output of the others,
    # so it seems we need to do some low level stuff here :-/.
    site_buf: dict[str, str] = {}
    while parallel and processes:
        for site_id, p in processes[:]:
            if p.stdout is None:
                raise Exception("stdout needs to be set")

            buf = site_buf.get(site_id, "")
            try:
                while True:
                    b = p.stdout.read(1024)
                    if not b:
                        break
                    buf += b
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    pass
                else:
                    raise

            while True:
                pos = buf.find("\n")
                if pos == -1:
                    break
                line, buf = buf[: pos + 1], buf[pos + 1 :]
                parallel_output(site_id, line)

            site_buf[site_id] = buf

            if not buf and p.poll() is not None:
                exit_states.append(p.returncode)
                processes.remove((site_id, p))
        time.sleep(0.01)

    # Do not simply take the highest exit code from the single sites.
    # We want to be able to output the fact that either none of the
    # sites is running or just some of the sites. For this we transform
    # the sites states 1 (not running) to 2 (partially running) if at least
    # one other site has state 0 (running) or 2 (partially running).
    if 1 in exit_states and (0 in exit_states or 2 in exit_states):
        exit_status = 2  # not all sites running, but at least one
    elif exit_states:
        exit_status = max(exit_states)
    else:
        exit_status = 0  # No OMD site existing

    sys.exit(exit_status)


def _update_license_usage(site: SiteContext) -> None:
    subprocess.Popen(  # pylint: disable=consider-using-with
        [f"/omd/sites/{site.name}/bin/cmk-update-license-usage"],
        start_new_session=True,
    )


def main_config(  # pylint: disable=too-many-branches
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if (not args or args[0] != "show") and not site.is_stopped() and global_opts.force:
        need_start = True
        stop_site(site)
    else:
        need_start = False

    config_hooks = load_config_hooks(site)
    set_hooks: list[str] = []
    if len(args) == 0:
        set_hooks = list(config_configure(site, global_opts, config_hooks))
    else:
        command = args[0]
        args = args[1:]
        if command == "show":
            config_show(site, config_hooks, args)
        elif command == "set":
            set_hooks = config_set(site, config_hooks, args)
        elif command == "change":
            set_hooks = config_change(version_info, site, config_hooks)
        else:
            config_usage()

    if set(set_hooks).intersection({"APACHE_TCP_ADDR", "APACHE_TCP_PORT", "APACHE_MODE"}):
        sys.stdout.write(
            f"WARNING: You have to execute 'omd update-apache-config {site.name}' as "
            "root to update and apply the configuration of the system apache.\n"
        )

    if need_start:
        start_site(version_info, site)


def main_su(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    try:
        os.execl("/bin/su", "su", "-", "%s" % site.name)
    except OSError:
        bail_out("Cannot open a shell for user %s" % site.name)


def _try_backup_site_to_tarfile(
    fh: io.BufferedWriter | BinaryIO,
    tar_mode: str,
    options: CommandOptions,
    site: SiteContext,
    global_opts: GlobalOptions,
) -> None:
    if "no-compression" not in options:
        tar_mode += "gz"

    try:
        omdlib.backup.backup_site_to_tarfile(site, fh, tar_mode, options, global_opts.verbose)
    except OSError as e:
        bail_out("Failed to perform backup: %s" % e)


def main_backup(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if len(args) == 0:
        bail_out(
            "You need to provide either a path to the destination "
            'file or "-" for backup to stdout.'
        )

    dest = args[0]

    if dest == "-":
        _try_backup_site_to_tarfile(sys.stdout.buffer, "w|", options, site, global_opts)
    else:
        if not (dest_path := Path(dest)).is_absolute():
            dest_path = global_opts.orig_working_directory / dest_path
        with dest_path.open(mode="wb") as fh:
            _try_backup_site_to_tarfile(fh, "w:", options, site, global_opts)


def _restore_backup_from_tar(  # pylint: disable=too-many-branches
    *,
    tar: tarfile.TarFile,
    site: SiteContext,
    options: CommandOptions,
    global_opts: GlobalOptions,
    version_info: VersionInfo,
    source_descr: str,
    new_site_name: str | None,
) -> SiteContext:
    try:
        sitename, version = omdlib.backup.get_site_and_version_from_backup(tar)
    except Exception as e:
        bail_out("%s" % e)

    if not version_exists(version):
        bail_out(
            "You need to have version %s installed to be able to restore " "this backup." % version
        )

    if is_root():
        # Ensure the restore is done with the sites version
        if version != omdlib.__version__:
            exec_other_omd(site, version, "restore")

        # Restore site with its original name, or specify a new one
        new_sitename = new_site_name or sitename
    else:
        new_sitename = site_name()

    site = SiteContext(new_sitename)

    if is_root():
        sys.stdout.write(f"Restoring site {site.name} from {source_descr}...\n")
        sys.stdout.flush()

        prepare_restore_as_root(version_info, site, options)

    else:
        sys.stdout.write("Restoring site from %s...\n" % source_descr)
        sys.stdout.flush()

        site.load_config(load_defaults(site))
        orig_apache_port = site.conf["APACHE_TCP_PORT"]

        prepare_restore_as_site_user(site, global_opts, options)

    # Now extract all files
    for tarinfo in tar:
        # The files in the tar archive start with the siteid as first element.
        # Remove this first element from the file paths and also care for hard link
        # targets.

        # Remove leading site name from paths
        tarinfo.name = "/".join(tarinfo.name.split("/")[1:])
        if global_opts.verbose:
            sys.stdout.write("Restoring %s...\n" % tarinfo.name)

        if tarinfo.islnk():
            parts = tarinfo.linkname.split("/")

            if parts[0] == sitename:
                new_linkname = "/".join(parts[1:])

                if global_opts.verbose:
                    sys.stdout.write(
                        f"  Rewriting link target from {tarinfo.linkname} to {new_linkname}\n"
                    )
                tarinfo.linkname = new_linkname

        tar.extract(tarinfo, path=site.dir)

    site.load_config(load_defaults(site))

    # give new user all files
    chown_tree(site.dir, site.name)

    # Change config files from old to new site (see rename_site())
    if sitename != site.name:
        old_site = SiteContext(sitename)
        patch_skeleton_files(_get_conflict_mode(options), old_site, site)

    # Now switch over to the new site as currently active site
    os.chdir(site.dir)
    set_environment(site)

    # Needed by the post-rename-site script
    putenv("OLD_OMD_SITE", sitename)

    if is_root():
        postprocess_restore_as_root(version_info, site, options)
    else:
        postprocess_restore_as_site_user(version_info, site, options, orig_apache_port)

    return site


def main_restore(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    if len(args) == 0:
        bail_out(
            'You need to provide either a path to the source file or "-" for restore from stdin.'
        )

    source = args[-1]
    source_descr = "stdin" if source == "-" else source
    new_site_name = args[0] if len(args) == 2 else None

    name = None
    fileobj = None

    if source == "-":
        fileobj = sys.stdin.buffer
        mode = "r|*"
    elif (source_path := Path(source)).exists():
        name = source_path
        mode = "r:*"
    else:
        bail_out("The backup archive does not exist.")

    try:
        with tarfile.open(
            name=name,
            fileobj=fileobj,
            mode=mode,
        ) as tar:
            site = _restore_backup_from_tar(
                tar=tar,
                site=site,
                options=options,
                global_opts=global_opts,
                version_info=version_info,
                source_descr=source_descr,
                new_site_name=new_site_name,
            )
    except tarfile.ReadError as e:
        bail_out("Failed to open the backup: %s" % e)


def prepare_restore_as_root(
    version_info: VersionInfo, site: SiteContext, options: CommandOptions
) -> None:
    reuse = False
    if "reuse" in options:
        reuse = True
        if not user_verify(version_info, site, allow_populated=True):
            bail_out("Error verifying site user.")
        fstab_verify(site)

    sitename_must_be_valid(site, reuse)

    if reuse:
        if not site.is_stopped() and "kill" not in options:
            bail_out("Cannot restore '%s' while it is running." % (site.name))
        else:
            with subprocess.Popen(["omd", "stop", site.name]):
                pass
        unmount_tmpfs(site, kill="kill" in options)

    if not reuse:
        uid = options.get("uid")
        gid = options.get("gid")
        useradd(version_info, site, uid, gid)  # None for uid/gid means: let Linux decide
    else:
        sys.stdout.write("Deleting existing site data...\n")
        shutil.rmtree(site.dir)
        ok()

    os.mkdir(site.dir)


def prepare_restore_as_site_user(
    site: SiteContext, global_opts: GlobalOptions, options: CommandOptions
) -> None:
    if not site.is_stopped() and "kill" not in options:
        bail_out("Cannot restore site while it is running.")

    verify_directory_write_access(site)

    sys.stdout.write("Stopping site processes...\n")
    stop_site(site)
    kill_site_user_processes(site, global_opts, exclude_current_and_parents=True)
    ok()

    unmount_tmpfs(site)

    sys.stdout.write("Deleting existing site data...")
    for f in os.listdir(site.dir):
        path = site.dir + "/" + f
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
    ok()


# Scans all site directories and ensures the site user is able to write all directories.
# This is needed to prevent eventual permission issues during the rmtree process.
def verify_directory_write_access(site: SiteContext) -> None:
    wrong = []
    for dirpath, dirnames, _filenames in os.walk(site.dir):
        for dirname in dirnames:
            path = dirpath + "/" + dirname
            if os.path.islink(path):
                continue

            if not os.access(path, os.W_OK):
                wrong.append(path)

    if wrong:
        bail_out(
            "Unable to start restore because of a permission issue.\n\n"
            "The restore needs to be able to clean the whole site to be able to restore "
            "the backup. Missing write access on the following paths:\n\n"
            "    %s" % "\n    ".join(wrong)
        )


def terminate_site_user_processes(site: SiteContext, global_opts: GlobalOptions) -> None:
    """Sends a SIGTERM to all running site processes and waits up to 5 seconds for termination

    In case one or more processes are still running after the timeout, the method will make
    the current OMD call terminate.
    """

    pids = site_user_processes(site, exclude_current_and_parents=True)
    if not pids:
        return

    sys.stdout.write("Stopping %d remaining site processes..." % len(pids))

    timeout_at = time.time() + 5
    sent_terminate = False
    while pids and time.time() < timeout_at:
        for pid in pids[:]:
            try:
                if not sent_terminate:
                    if global_opts.verbose:
                        sys.stdout.write("%d..." % pid)
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIG_DFL)
            except OSError as e:
                if e.errno == errno.ESRCH:  # No such process
                    pids.remove(pid)
                else:
                    raise

        sent_terminate = True
        time.sleep(0.1)

    if pids:
        bail_out("\nFailed to stop remaining site processes: %s" % ", ".join(map(str, pids)))
    else:
        ok()


def kill_site_user_processes(
    site: SiteContext, global_opts: GlobalOptions, exclude_current_and_parents: bool = False
) -> None:
    pids = site_user_processes(site, exclude_current_and_parents)
    tries = 5
    while tries > 0 and pids:
        for pid in pids[:]:
            try:
                logger.log(VERBOSE, "Killing process %d...", pid)
                os.kill(pid, signal.SIGKILL)
            except OSError as e:
                if e.errno == errno.ESRCH:
                    pids.remove(pid)  # No such process
                else:
                    raise
        time.sleep(1)
        tries -= 1

    if pids:
        bail_out("Failed to kill site processes: %s" % ", ".join(map(str, pids)))


def get_current_and_parent_pids() -> list[int]:
    """Return list of PIDs of the current process and parent process tree till pid 0"""
    pids = []
    process = psutil.Process()
    while process and process.pid != 0:
        pids.append(process.pid)
        process = process.parent()
    return pids


def site_user_processes(site: SiteContext, exclude_current_and_parents: bool) -> list[int]:
    """Return list of PIDs of all running site user processes (that are not excluded)"""
    exclude: list[int] = []
    if exclude_current_and_parents:
        exclude = get_current_and_parent_pids()
    with subprocess.Popen(
        ["ps", "-U", site.name, "-o", "pid", "--no-headers"],
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        encoding="utf-8",
    ) as user_process:
        exclude.append(user_process.pid)
        pids = []
        for l in user_process.communicate()[0].split("\n"):
            line = l.strip()
            if not line:
                continue
            pid = int(line)
            if pid in exclude:
                continue
            pids.append(pid)
    return pids


def postprocess_restore_as_root(
    version_info: VersionInfo, site: SiteContext, options: CommandOptions
) -> None:
    # Entry for tmps in /etc/fstab
    if "reuse" in options:
        command_type = CommandType.restore_existing_site
    else:
        command_type = CommandType.restore_as_new_site
        add_to_fstab(site, tmpfs_size=options.get("tmpfs-size"))

    finalize_site(version_info, site, command_type, "apache-reload" in options)


def postprocess_restore_as_site_user(
    version_info: VersionInfo, site: SiteContext, options: CommandOptions, orig_apache_port: str
) -> None:
    # Keep the apache port the site currently being replaced had before
    # (we can not restart the system apache as site user)
    site.conf["APACHE_TCP_PORT"] = orig_apache_port
    save_site_conf(site)

    finalize_site_as_user(
        version_info,
        site,
        (
            CommandType.restore_existing_site
            if "reuse" in options
            else CommandType.restore_as_new_site
        ),
    )


def main_cleanup(
    version_info: VersionInfo,
    site: SiteContext,
    global_opts: GlobalOptions,
    args: Arguments,
    options: CommandOptions,
) -> None:
    package_manager = PackageManager.factory(version_info)
    if package_manager is None:
        bail_out("Command is not supported on this platform")

    all_installed_packages = package_manager.get_all_installed_packages()

    for version in omd_versions():
        if version == default_version():
            sys.stdout.write(
                "%s%-20s%s Keeping this version, since it is the default.\n"
                % (
                    tty.bold,
                    version,
                    tty.normal,
                ),
            )
            continue

        site_ids = [s for s in all_sites() if SiteContext(s).version == version]
        if site_ids:
            sys.stdout.write(
                "%s%-20s%s In use (by %s). Keeping this version.\n"
                % (tty.bold, version, tty.normal, ", ".join(site_ids))
            )
            continue

        target_package_name = "%s-%s" % (
            _get_edition(version),
            _get_raw_version(version),
        )

        matching_installed_packages = [
            package for package in all_installed_packages if target_package_name in package
        ]

        if len(matching_installed_packages) != 1:
            sys.stdout.write(
                "%s%-20s%s Could not determine package. Keeping this version.\n"
                % (tty.bold, version, tty.normal)
            )
            continue

        sys.stdout.write("%s%-20s%s Uninstalling\n" % (tty.bold, version, tty.normal))
        package_manager.uninstall(matching_installed_packages[0])

        # In case there were modifications made to the version the uninstall may leave
        # some files behind. Remove the whole version directory
        version_path: str = os.path.join("/omd/versions", version)
        if os.path.exists(version_path):
            shutil.rmtree(version_path)

    # In case the last version has been removed ensure some things created globally
    # are removed.
    if not omd_versions():
        _cleanup_global_files(version_info)


def _cleanup_global_files(version_info: VersionInfo) -> None:
    sys.stdout.write("No version left. Cleaning up global files.\n")
    shutil.rmtree(version_info.OMD_PHYSICAL_BASE, ignore_errors=True)

    for path in [
        "/omd",
        version_info.APACHE_CONF_DIR + "/zzz_omd.conf",
        "/etc/init.d/omd",
        "/usr/bin/omd",
    ]:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    if group_exists("omd"):
        groupdel("omd")


class PackageManager(abc.ABC):
    @classmethod
    def factory(cls, version_info: VersionInfo) -> PackageManager | None:
        if os.path.exists("/etc/cma"):
            return None

        distro_code = version_info.DISTRO_CODE
        if distro_code.startswith("el") or distro_code.startswith("sles"):
            return PackageManagerRPM()
        return PackageManagerDEB()

    @abc.abstractmethod
    def uninstall(self, package_name: str) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def get_all_installed_packages(self) -> list[str]:
        raise NotImplementedError()

    def _execute_uninstall(self, cmd: list[str]) -> None:
        p = self._execute(cmd)
        output = p.communicate()[0]
        if p.wait() != 0:
            bail_out("Failed to uninstall package:\n%s" % output)

    def _execute(self, cmd: list[str]) -> subprocess.Popen:
        logger.log(VERBOSE, "Executing: %s", subprocess.list2cmdline(cmd))

        return subprocess.Popen(
            cmd,
            shell=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
        )


class PackageManagerDEB(PackageManager):
    def uninstall(self, package_name: str) -> None:
        self._execute_uninstall(["apt-get", "-y", "purge", package_name])

    def get_all_installed_packages(self) -> list[str]:
        p = self._execute(["dpkg", "-l"])
        output = p.communicate()[0]
        if p.wait() != 0:
            bail_out("Failed to get all installed packages:\n%s" % output)

        packages: list[str] = []
        for package in output.split("\n"):
            if not package.startswith("ii"):
                continue

            packages.append(package.split()[1])

        return packages


class PackageManagerRPM(PackageManager):
    def uninstall(self, package_name: str) -> None:
        self._execute_uninstall(["rpm", "-e", package_name])

    def get_all_installed_packages(self) -> list[str]:
        p = self._execute(["rpm", "-qa"])
        output = p.communicate()[0]

        if p.wait() != 0:
            bail_out("Failed to find packages:\n%s" % output)

        packages: list[str] = []
        for package in output.split("\n"):
            packages.append(package)

        return packages


class Option(NamedTuple):
    long_opt: str
    short_opt: str | None
    needs_arg: bool
    description: str


exclude_options = [
    Option("no-rrds", None, False, "do not copy RRD files (performance data)"),
    Option("no-logs", None, False, "do not copy the monitoring history and log files"),
    Option("no-past", "N", False, "do not copy RRD files, the monitoring history and log files"),
]


#  command       The id of the command
#  only_root     This option is only available when omd command is run as root
#  no_suid       The command is available for root and site-user, but no switch
#                to the site user is performed before execution the mode function
#  needs_site    When run as root:
#                0: No site must be specified
#                1: A site must be specified
#                2: A site is optional
#  must_exist    Site must be existant for this command
#  confirm       Is a confirm dialog shown before command execution?
#  args          Help text for command individual arguments
#  function      Handler function for this command
#  options_spec  List of individual arguments for this command
#  description   Text for the help of omd
#  confirm_text  Confirm text to show before calling the handler function
class Command(NamedTuple):
    command: str
    only_root: bool
    no_suid: bool
    needs_site: int
    # TODO: Refactor to bool
    site_must_exist: int
    confirm: bool
    args_text: str
    handler: Callable
    options: list[Option]
    description: str
    confirm_text: str


COMMANDS: Final = [
    Command(
        command="help",
        only_root=False,
        no_suid=False,
        needs_site=0,
        site_must_exist=0,
        confirm=False,
        args_text="",
        handler=main_help,
        options=[],
        description="Show general help",
        confirm_text="",
    ),
    Command(
        command="setversion",
        only_root=True,
        no_suid=False,
        needs_site=0,
        site_must_exist=0,
        confirm=False,
        args_text="VERSION",
        handler=main_setversion,
        options=[],
        description="Sets the default version of OMD which will be used by new sites",
        confirm_text="",
    ),
    Command(
        command="version",
        only_root=False,
        no_suid=False,
        needs_site=0,
        site_must_exist=0,
        confirm=False,
        args_text="[SITE]",
        handler=main_version,
        options=[
            Option("bare", "b", False, "output plain text optimized for parsing"),
        ],
        description="Show version of OMD",
        confirm_text="",
    ),
    Command(
        command="versions",
        only_root=False,
        no_suid=False,
        needs_site=0,
        site_must_exist=0,
        confirm=False,
        args_text="",
        handler=main_versions,
        options=[
            Option("bare", "b", False, "output plain text optimized for parsing"),
        ],
        description="List installed OMD versions",
        confirm_text="",
    ),
    Command(
        command="sites",
        only_root=False,
        no_suid=False,
        needs_site=0,
        site_must_exist=0,
        confirm=False,
        args_text="",
        handler=main_sites,
        options=[
            Option("bare", "b", False, "output plain text for easy parsing"),
        ],
        description="Show list of sites",
        confirm_text="",
    ),
    Command(
        command="create",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=0,
        confirm=False,
        args_text="",
        handler=main_create,
        options=[
            Option("uid", "u", True, "create site user with UID ARG"),
            Option("gid", "g", True, "create site group with GID ARG"),
            Option("admin-password", None, True, "set initial password instead of generating one"),
            Option("reuse", None, False, "do not create a site user, reuse existing one"),
            Option(
                "no-init", "n", False, "leave new site directory empty (a later omd init does this"
            ),
            Option("no-autostart", "A", False, "set AUTOSTART to off (useful for test sites)"),
            Option(
                "apache-reload",
                None,
                False,
                "Issue a reload of the system apache instead of a restart",
            ),
            Option("no-tmpfs", None, False, "set TMPFS to off"),
            Option(
                "tmpfs-size",
                "t",
                True,
                "specify the maximum size of the tmpfs (defaults to 50% of RAM), examples: 500M, 20G, 60%",
            ),
        ],
        description="Create a new site (-u UID, -g GID)",
        confirm_text="This command performs the following actions on your system:\n"
        "- Create the system user <SITENAME>\n"
        "- Create the system group <SITENAME>\n"
        "- Create and populate the site home directory\n"
        "- Restart the system wide apache daemon\n"
        "- Add tmpfs for the site to fstab and mount it",
    ),
    Command(
        command="init",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="",
        handler=main_init,
        options=[
            Option(
                "apache-reload",
                None,
                False,
                "Issue a reload of the system apache instead of a restart",
            ),
        ],
        description="Populate site directory with default files and enable the site",
        confirm_text="",
    ),
    Command(
        command="rm",
        only_root=True,
        no_suid=True,
        needs_site=1,
        site_must_exist=1,
        confirm=True,
        args_text="",
        handler=main_rm,
        options=[
            Option("reuse", None, False, "assume --reuse on create, do not delete site user/group"),
            Option("kill", None, False, "kill processes of the site before deleting it"),
            Option(
                "apache-reload",
                None,
                False,
                "Issue a reload of the system apache instead of a restart",
            ),
        ],
        description="Remove a site (and its data)",
        confirm_text="PLEASE NOTE: This action removes all configuration files\n"
        "             and variable data of the site.\n"
        "\n"
        "In detail the following steps will be done:\n"
        "- Stop all processes of the site\n"
        "- Unmount tmpfs of the site\n"
        "- Remove tmpfs of the site from fstab\n"
        "- Remove the system user <SITENAME>\n"
        "- Remove the system group <SITENAME>\n"
        "- Remove the site home directory\n"
        "- Restart the system wide apache daemon\n",
    ),
    Command(
        command="disable",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="",
        handler=main_disable,
        options=[
            Option("kill", None, False, "kill processes using tmpfs before unmounting it"),
        ],
        description="Disable a site (stop it, unmount tmpfs, remove Apache hook)",
        confirm_text="",
    ),
    Command(
        command="enable",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="",
        handler=main_enable,
        options=[],
        description="Enable a site (reenable a formerly disabled site)",
        confirm_text="",
    ),
    Command(
        command="update-apache-config",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="",
        handler=main_update_apache_config,
        options=[],
        description="Update the system apache config of a site (and reload apache)",
        confirm_text="",
    ),
    Command(
        command="mv",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="NEWNAME",
        handler=lambda version_info, site, global_opts, args_text, opts: main_mv_or_cp(
            version_info, site, global_opts, CommandType.move, args_text, opts
        ),
        options=[
            Option("uid", "u", True, "create site user with UID ARG"),
            Option("gid", "g", True, "create site group with GID ARG"),
            Option("reuse", None, False, "do not create a site user, reuse existing one"),
            Option(
                "conflict",
                None,
                True,
                "non-interactive conflict resolution. ARG is install, keepold, abort or ask",
            ),
            Option(
                "tmpfs-size",
                "t",
                True,
                "specify the maximum size of the tmpfs (defaults to 50% of RAM), examples: 500M, 20G, 60%",
            ),
            Option(
                "apache-reload",
                None,
                False,
                "Issue a reload of the system apache instead of a restart",
            ),
        ],
        description="Rename a site",
        confirm_text="",
    ),
    Command(
        command="cp",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="NEWNAME",
        handler=lambda version_info, site, global_opts, args_text, opts: main_mv_or_cp(
            version_info, site, global_opts, CommandType.copy, args_text, opts
        ),
        options=[
            Option("uid", "u", True, "create site user with UID ARG"),
            Option("gid", "g", True, "create site group with GID ARG"),
            Option("reuse", None, False, "do not create a site user, reuse existing one"),
        ]
        + exclude_options
        + [
            Option(
                "conflict",
                None,
                True,
                "non-interactive conflict resolution. ARG is install, keepold, abort or ask",
            ),
            Option(
                "tmpfs-size",
                "t",
                True,
                "specify the maximum size of the tmpfs (defaults to 50% of RAM), examples: 500M, 20G, 60%",
            ),
            Option(
                "apache-reload",
                None,
                False,
                "Issue a reload of the system apache instead of a restart",
            ),
        ],
        description="Make a copy of a site",
        confirm_text="",
    ),
    Command(
        command="update",
        only_root=False,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="",
        handler=main_update,
        options=[
            Option(
                "conflict",
                None,
                True,
                "non-interactive conflict resolution. ARG is install, keepold, abort or ask",
            )
        ],
        description="Update site to other version of OMD",
        confirm_text="",
    ),
    Command(
        command="start",
        only_root=False,
        no_suid=False,
        needs_site=2,
        site_must_exist=1,
        confirm=False,
        args_text="[SERVICE]",
        handler=lambda version_info, site, global_opts, args_text, opts: main_init_action(
            version_info, site, global_opts, "start", args_text, opts
        ),
        options=[
            Option("version", "V", True, "only start services having version ARG"),
            Option("parallel", "p", False, "Invoke start of sites in parallel"),
        ],
        description="Start services of one or all sites",
        confirm_text="",
    ),
    Command(
        command="stop",
        only_root=False,
        no_suid=False,
        needs_site=2,
        site_must_exist=1,
        confirm=False,
        args_text="[SERVICE]",
        handler=lambda version_info, site, global_opts, args_text, opts: main_init_action(
            version_info, site, global_opts, "stop", args_text, opts
        ),
        options=[
            Option("version", "V", True, "only stop sites having version ARG"),
            Option("parallel", "p", False, "Invoke stop of sites in parallel"),
        ],
        description="Stop services of site(s)",
        confirm_text="",
    ),
    Command(
        command="restart",
        only_root=False,
        no_suid=False,
        needs_site=2,
        site_must_exist=1,
        confirm=False,
        args_text="[SERVICE]",
        handler=lambda version_info, site, global_opts, args_text, opts: main_init_action(
            version_info, site, global_opts, "restart", args_text, opts
        ),
        options=[
            Option("version", "V", True, "only restart sites having version ARG"),
        ],
        description="Restart services of site(s)",
        confirm_text="",
    ),
    Command(
        command="reload",
        only_root=False,
        no_suid=False,
        needs_site=2,
        site_must_exist=1,
        confirm=False,
        args_text="[SERVICE]",
        handler=lambda version_info, site, global_opts, args_text, opts: main_init_action(
            version_info, site, global_opts, "reload", args_text, opts
        ),
        options=[
            Option("version", "V", True, "only reload sites having version ARG"),
        ],
        description="Reload services of site(s)",
        confirm_text="",
    ),
    Command(
        command="status",
        only_root=False,
        no_suid=False,
        needs_site=2,
        site_must_exist=1,
        confirm=False,
        args_text="[SERVICE]",
        handler=lambda version_info, site, global_opts, args_text, opts: main_init_action(
            version_info, site, global_opts, "status", args_text, opts
        ),
        options=[
            Option("version", "V", True, "show only sites having version ARG"),
            Option("auto", None, False, "show only sites with AUTOSTART = on"),
            Option("bare", "b", False, "output plain format optimized for parsing"),
        ],
        description="Show status of services of site(s)",
        confirm_text="",
    ),
    Command(
        command="config",
        only_root=False,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="...",
        handler=main_config,
        options=[],
        description="Show and set site configuration parameters.\n\n\
Usage:\n\
 omd config [site]\t\t\tinteractive mode\n\
 omd config [site] show\t\t\tshow configuration settings\n\
 omd config [site] set VAR VAL\t\tset specific setting VAR to VAL",
        confirm_text="",
    ),
    Command(
        command="diff",
        only_root=False,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="([RELBASE])",
        handler=main_diff,
        options=[
            Option("bare", "b", False, "output plain diff format, no beautifying"),
        ],
        description="Shows differences compared to the original version files",
        confirm_text="",
    ),
    Command(
        command="su",
        only_root=True,
        no_suid=False,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="",
        handler=main_su,
        options=[],
        description="Run a shell as a site-user",
        confirm_text="",
    ),
    Command(
        command="umount",
        only_root=False,
        no_suid=False,
        needs_site=2,
        site_must_exist=1,
        confirm=False,
        args_text="",
        handler=main_umount,
        options=[
            Option("version", "V", True, "unmount only sites with version ARG"),
            Option("kill", None, False, "kill processes using the tmpfs before unmounting it"),
        ],
        description="Umount ramdisk volumes of site(s)",
        confirm_text="",
    ),
    Command(
        command="backup",
        only_root=False,
        no_suid=True,
        needs_site=1,
        site_must_exist=1,
        confirm=False,
        args_text="[SITE] [-|ARCHIVE_PATH]",
        handler=main_backup,
        options=exclude_options
        + [
            Option("no-compression", None, False, "do not compress tar archive"),
        ],
        description="Create a backup tarball of a site, writing it to a file or stdout",
        confirm_text="",
    ),
    Command(
        command="restore",
        only_root=False,
        no_suid=False,
        needs_site=0,
        site_must_exist=0,
        confirm=False,
        args_text="[SITE] handler=[-|ARCHIVE_PATH]",
        handler=main_restore,
        options=[
            Option("uid", "u", True, "create site user with UID ARG"),
            Option("gid", "g", True, "create site group with GID ARG"),
            Option("reuse", None, False, "do not create a site user, reuse existing one"),
            Option(
                "kill",
                None,
                False,
                "kill processes of site when reusing an existing one before restoring",
            ),
            Option(
                "apache-reload",
                None,
                False,
                "Issue a reload of the system apache instead of a restart",
            ),
            Option(
                "conflict",
                None,
                True,
                "non-interactive conflict resolution. ARG is install, keepold, abort or ask",
            ),
            Option(
                "tmpfs-size",
                "t",
                True,
                "specify the maximum size of the tmpfs (defaults to 50% of RAM)",
            ),
        ],
        description="Restores the backup of a site to an existing site or creates a new site",
        confirm_text="",
    ),
    Command(
        command="cleanup",
        only_root=True,
        no_suid=False,
        needs_site=0,
        site_must_exist=0,
        confirm=False,
        args_text="",
        handler=main_cleanup,
        options=[],
        description="Uninstall all Check_MK versions that are not used by any site.",
        confirm_text="",
    ),
]


class GlobalOptions(NamedTuple):
    verbose: bool
    force: bool
    interactive: bool
    orig_working_directory: str


def handle_global_option(
    global_opts: GlobalOptions, main_args: Arguments, opt: str, orig: str
) -> tuple[GlobalOptions, Arguments]:
    verbose = global_opts.verbose
    force = global_opts.force
    interactive = global_opts.interactive

    if opt in ["V", "version"]:
        # Switch to other version of bin/omd
        version, main_args = _opt_arg(main_args, opt)
        if version != omdlib.__version__:
            omd_path = "/omd/versions/%s/bin/omd" % version
            if not os.path.exists(omd_path):
                bail_out("OMD version '%s' is not installed." % version)
            os.execv(omd_path, sys.argv)
            bail_out("Cannot execute %s." % omd_path)
    elif opt in ["f", "force"]:
        force = True
        interactive = False
    elif opt in ["i", "interactive"]:
        force = False
        interactive = True
    elif opt in ["v", "verbose"]:
        verbose = True
    else:
        bail_out("Invalid global option %s.\n" "Call omd help for available options." % orig)

    new_global_opts = GlobalOptions(
        verbose=verbose,
        force=force,
        interactive=interactive,
        orig_working_directory=global_opts.orig_working_directory,
    )

    return new_global_opts, main_args


def _opt_arg(main_args: Arguments, opt: str) -> tuple[str, Arguments]:
    if len(main_args) < 1:
        bail_out("Option %s needs an argument." % opt)
    arg = main_args[0]
    main_args = main_args[1:]
    return arg, main_args


def _parse_command_options(  # pylint: disable=too-many-branches
    args: Arguments, options: list[Option]
) -> tuple[Arguments, CommandOptions]:
    # Give a short overview over the command specific options
    # when the user specifies --help:
    if len(args) and args[0] in ["-h", "--help"]:
        if options:
            sys.stdout.write("Possible options for this command:\n")
        else:
            sys.stdout.write("No options for this command\n")
        for option in options:
            args_text = "{}--{}".format(
                "-%s," % option.short_opt if option.short_opt else "",
                option.long_opt,
            )
            sys.stdout.write(
                " %-15s %3s  %s\n"
                % (args_text, option.needs_arg and "ARG" or "", option.description)
            )
        sys.exit(0)

    set_options: CommandOptions = {}

    while len(args) >= 1 and args[0][0] == "-" and len(args[0]) > 1:
        opt = args[0]
        args = args[1:]

        found_options: list[Option] = []
        if opt.startswith("--"):
            # Handle --foo=bar
            if "=" in opt:
                opt, optarg = opt.split("=", 1)
                args = [optarg] + args
                for option in options:
                    if option.long_opt == opt[2:] and not option.needs_arg:
                        bail_out("The option %s does not take an argument" % opt)

            for option in options:
                if option.long_opt == opt[2:]:
                    found_options = [option]
        else:
            for char in opt:
                for option in options:
                    if option.short_opt == char:
                        found_options.append(option)

        if not found_options:
            bail_out("Invalid option '%s'" % opt)

        for option in found_options:
            arg = None
            if option.needs_arg:
                if not args:
                    bail_out("Option '%s' needs an argument." % opt)
                arg = args[0]
                args = args[1:]
            set_options[option.long_opt] = arg
    return (args, set_options)


def exec_other_omd(site: SiteContext, version: str, command: str) -> NoReturn:
    # Rerun with omd of other version
    omd_path = "/omd/versions/%s/bin/omd" % version
    if os.path.exists(omd_path):
        if command == "update":
            # Prevent inheriting environment variables from this versions/site environment
            # into the execed omd call. The OMD call must import the python version related
            # modules and libaries. This only works when PYTHONPATH and LD_LIBRARY_PATH are
            # not already set when calling "omd update"
            try:
                del os.environ["PYTHONPATH"]
            except KeyError:
                pass

            try:
                del os.environ["LD_LIBRARY_PATH"]
            except KeyError:
                pass

        os.execv(omd_path, sys.argv)
        bail_out("Cannot run bin/omd of version %s." % version)
    else:
        bail_out(
            "Site %s uses version %s which is not installed.\n"
            "Please reinstall that version and retry this command." % (site.name, version)
        )


def ensure_mkbackup_lock_dir_rights() -> None:
    try:
        mkbackup_lock_dir.mkdir(mode=0o0770, exist_ok=True)
        shutil.chown(mkbackup_lock_dir, group="omd")
        mkbackup_lock_dir.chmod(0o0770)
    except PermissionError:
        logger.log(
            VERBOSE,
            "Unable to create %s needed for mkbackup. "
            "This may be due to the fact that your SITE "
            "User isn't allowed to create the backup directory. "
            "You could resolve this issue by running 'sudo omd start' as root "
            "(and not as SITE user).",
            mkbackup_lock_dir,
        )


# .
#   .--Main----------------------------------------------------------------.
#   |                        __  __       _                                |
#   |                       |  \/  | __ _(_)_ __                           |
#   |                       | |\/| |/ _` | | '_ \                          |
#   |                       | |  | | (_| | | | | |                         |
#   |                       |_|  |_|\__,_|_|_| |_|                         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Main entry point                                                    |
#   '----------------------------------------------------------------------'


# Handle global options. We might convert this to getopt
# later. But a problem here is that we have options appearing
# *before* the command and command specific ones. We handle
# the options before the command here only
# TODO: Refactor these global variables
# TODO: Refactor to argparse. Be aware of the pitfalls of the OMD command line scheme
def main() -> None:  # pylint: disable=too-many-branches
    ensure_mkbackup_lock_dir_rights()

    main_args = sys.argv[1:]
    site: AbstractSiteContext = RootContext()

    version_info = VersionInfo(omdlib.__version__)
    version_info.load()

    global_opts = default_global_options()
    while len(main_args) >= 1 and main_args[0].startswith("-"):
        opt = main_args[0]
        main_args = main_args[1:]
        if opt.startswith("--"):
            global_opts, main_args = handle_global_option(global_opts, main_args, opt[2:], opt)
        else:
            for c in opt[1:]:
                global_opts, main_args = handle_global_option(global_opts, main_args, c, opt)

    if len(main_args) < 1:
        main_help(version_info, site, global_opts)
        sys.exit(1)

    args = main_args[1:]

    if global_opts.verbose:
        logger.setLevel(VERBOSE)

    command = _get_command(version_info, site, global_opts, main_args[0])

    if not is_root() and command.only_root:
        bail_out("omd: root permissions are needed for this command.")

    # Parse command options. We need to do this now in order to know,
    # if a site name has been specified or not

    # Give a short description for the command when the user specifies --help:
    if args and args[0] in ["-h", "--help"]:
        sys.stdout.write("%s\n\n" % command.description)
    args, command_options = _parse_command_options(args, command.options)

    # Some commands need a site to be specified. If we are
    # called as root, this must be done explicitely. If we
    # are site user, the site name is our user name
    if command.needs_site > 0:
        if is_root():
            if len(args) >= 1:
                site = SiteContext(args[0])
                args = args[1:]
            elif command.needs_site == 1:
                bail_out("omd: please specify site.")
        else:
            site = SiteContext(site_name())

    check_site_user(site, command.site_must_exist)

    # Commands operating on an existing site *must* run omd in
    # the same version as the site has! Sole exception: update.
    # That command must be run in the target version
    if site.is_site_context() and command.site_must_exist and command.command != "update":
        if not isinstance(site, SiteContext):
            raise Exception("site must be of type SiteContext")

        v = site.version
        if v is None:  # Site has no home directory or version link
            if command.command == "rm":
                sys.stdout.write(
                    "WARNING: This site has an empty home directory and is not\n"
                    "assigned to any OMD version. You are running version %s.\n"
                    % omdlib.__version__
                )
            elif command.command != "init":
                bail_out(
                    "This site has an empty home directory /omd/sites/%s.\n"
                    "If you have created that site with 'omd create --no-init %s'\n"
                    "then please first do an 'omd init %s'." % (3 * (site.name,))
                )
        elif omdlib.__version__ != v:
            exec_other_omd(site, v, command.command)

    if isinstance(site, SiteContext):
        site.load_config(load_defaults(site))

    # Commands which affect a site and can be called as root *or* as
    # site user should always run with site user privileges. That way
    # we are sure that new files and processes are created under the
    # site user and never as root.
    if not command.no_suid and site.is_site_context() and is_root() and not command.only_root:
        if not isinstance(site, SiteContext):
            raise Exception("site must be of type SiteContext")
        switch_to_site_user(site)

    # Make sure environment is in a defined state
    if site.is_site_context():
        if not isinstance(site, SiteContext):
            raise Exception("site must be of type SiteContext")
        clear_environment()
        set_environment(site)

    if (global_opts.interactive or command.confirm) and not global_opts.force:
        answer = None
        while answer not in ["", "yes", "no"]:
            answer = input(f"{command.confirm_text} [yes/NO]: ").strip().lower()
        if answer in ["", "no"]:
            bail_out(tty.normal + "Aborted.")

    try:
        command.handler(version_info, site, global_opts, args, command_options)
    except MKTerminate as e:
        bail_out(str(e))
    except KeyboardInterrupt:
        bail_out(tty.normal + "Aborted.")


def default_global_options() -> GlobalOptions:
    return GlobalOptions(
        verbose=False,
        force=False,
        interactive=False,
        orig_working_directory=_get_orig_working_directory(),
    )


def _get_command(
    version_info: VersionInfo,
    site: AbstractSiteContext,
    global_opts: GlobalOptions,
    command_arg: str,
) -> Command:
    for command in COMMANDS:
        if command.command == command_arg:
            return command

    sys.stderr.write("omd: no such command: %s\n" % command_arg)
    main_help(version_info, site, global_opts)
    sys.exit(1)


def _get_orig_working_directory() -> str:
    try:
        return os.getcwd()
    except FileNotFoundError:
        return "/"
