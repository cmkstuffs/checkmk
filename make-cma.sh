make clean
USE_EXTERNAL_PIPENV_MIRROR=true make cma  2>&1 | tee -a make-cma.sh.out
