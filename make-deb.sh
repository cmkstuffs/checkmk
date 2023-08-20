make clean
USE_EXTERNAL_PIPENV_MIRROR=true make 2>&1 | tee -a make-deb.sh.out
