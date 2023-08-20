export PATH=${HOME}/.local/bin:$PATH
make clean
>make-deb.sh.out
USE_EXTERNAL_PIPENV_MIRROR=true make deb 2>&1 | tee -a make-deb.sh.out
