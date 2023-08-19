# https://stackoverflow.com/questions/39399804/updates-were-rejected-because-the-tip-of-your-current-branch-is-behind-its-remot
#There is no tracking information for the current branch.
#Please specify which branch you want to merge with.
#See git-pull(1) for details.
#
#    git pull <remote> <branch>
#
#If you wish to set tracking information for this branch you can do so with:
#
#    git branch --set-upstream-to=origin/<branch> dev
# git config pull.ff only
#
# git pull --rebase
git branch --set-upstream-to=origin/dev  dev
