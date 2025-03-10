#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from typing import Any

from marshmallow import validates_schema, ValidationError

from cmk.utils.regex import WATO_FOLDER_PATH_NAME_REGEX

from cmk.gui import fields as gui_fields
from cmk.gui.fields.utils import BaseSchema

from cmk import fields

EXISTING_FOLDER = gui_fields.FolderField(
    example="/",
    required=True,
)


class CreateFolder(BaseSchema):
    """Creating a folder

    Every folder needs a parent folder to reside in. The uppermost folder is called the "root"
    Folder and has the fixed identifier "root".

    Parameters:

     * `name` is the actual folder-name on disk. This will be autogenerated from the title, if not given.
     * `title` is meant for humans to read.
     * `parent` is the identifier for the parent-folder. This identifier stays the same,
        even if the parent folder is being moved.
     * `attributes` can hold special configuration parameters which control various aspects of
        the monitoring system. Most of these attributes will be inherited by hosts within that
        folder. For more information please have a look at the
        [Host Administration chapter of the user guide](https://docs.checkmk.com/master/en/wato_hosts.html#intro).
    """

    name = fields.String(
        description=(
            "The filesystem directory name (not path!) of the folder. No slashes are allowed."
        ),
        pattern=WATO_FOLDER_PATH_NAME_REGEX,
        example="production",
        minLength=1,
    )
    title = fields.String(
        required=True,
        description="The folder title as displayed in the user interface.",
        example="Production Hosts",
    )
    parent = gui_fields.FolderField(
        required=True,
        description=(
            "The folder in which the new folder shall be placed in. The root-folder is "
            "specified by '/'."
        ),
        example="/",
    )
    attributes = gui_fields.host_attributes_field(
        "folder",
        "create",
        "inbound",
        required=False,
        description=(
            "Specific attributes to apply for all hosts in this folder " "(among other things)."
        ),
        example={"tag_criticality": "prod"},
    )


class BulkCreateFolder(BaseSchema):
    entries = fields.List(
        fields.Nested(CreateFolder),
        example=[
            {
                "name": "production",
                "parent": "root",
                "attributes": {"foo": "bar"},
            }
        ],
        uniqueItems=True,
    )


class UpdateFolder(BaseSchema):
    """Updating a folder"""

    schema_example = {"title": "Virtual Servers", "attributes": {"tag_networking": "wan"}}

    title = fields.String(
        example="Virtual Servers.",
        required=False,
        description="The title of the folder. Used in the GUI.",
    )
    attributes = gui_fields.host_attributes_field(
        "folder",
        "update",
        "inbound",
        description=(
            "Replace all attributes with the ones given in this field. Already set"
            "attributes, not given here, will be removed. Can't be used together with "
            "update_attributes or remove_attributes fields."
        ),
        example={"tag_networking": "wan"},
        required=False,
        load_default=None,
    )
    update_attributes = gui_fields.host_attributes_field(
        "folder",
        "update",
        "inbound",
        description=(
            "Only set the attributes which are given in this field. Already set "
            "attributes will not be touched. Can't be used together with attributes "
            "or remove_attributes fields."
        ),
        example={"tag_criticality": "prod"},
        required=False,
        load_default=None,
    )
    remove_attributes = fields.List(
        fields.String(),
        description=(
            "A list of attributes which should be removed. Can't be used together "
            "with attributes or update_attributes fields."
        ),
        example=["tag_foobar"],
        required=False,
    )

    @validates_schema
    def validate_attributes(self, data: dict[str, Any], **kwargs: Any) -> dict:
        """Only one of the attributes fields is allowed at a time"""
        only_one_of = {"attributes", "update_attributes", "remove_attributes"}

        attribute_fields_sent = only_one_of & set(data)
        if len(attribute_fields_sent) > 1:
            raise ValidationError(
                f"This endpoint only allows 1 action (set/update/remove) per call, you specified {len(attribute_fields_sent)} actions: {', '.join(attribute_fields_sent)}."
            )
        return data


class UpdateFolderEntry(UpdateFolder):
    folder = EXISTING_FOLDER


class BulkUpdateFolder(BaseSchema):
    entries = fields.Nested(
        UpdateFolderEntry,
        many=True,
        example=[
            {
                "remove_attributes": ["tag_foobar"],
            }
        ],
        description="A list of folder entries.",
        required=True,
    )


class MoveFolder(BaseSchema):
    destination = gui_fields.FolderField(
        required=True,
        description="Where the folder has to be moved to.",
        example="~my~fine/folder",
    )


class BulkDeleteFolder(BaseSchema):
    entries = fields.List(
        EXISTING_FOLDER,
        required=True,
        example=["production", "secondproduction"],
    )
