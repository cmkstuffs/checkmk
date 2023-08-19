wget https://musl.libc.org/releases/musl-1.2.4.tar.gz
tar xf musl-1.2.4.tar.gz && (cd musl-1.2.4 ;./configure  --prefix=/home/me/.local && make install)
