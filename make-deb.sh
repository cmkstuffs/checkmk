make clean
USE_EXTERNAL_PIPENV_MIRROR=true make deb 2>&1 | tee -a make-deb.sh.out
