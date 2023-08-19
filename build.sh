# for RL8
sudo python -m pip install pipenv
sudo dnf install -y libpq-devel npm libstdc++-static
rm -rf $1
tar xzf $1.tar.gz && cd $1
cp omd/distros/CENTOS_8.mk omd/distros/Rocky_Linux.mk
export PATH=/usr/pgsql-13/bin/:$PATH
#./configure --with-nagios4 # this will caused problem.
./configure 
clear 
make -C livestatus

clear
time make rpm | tee ../make-rpm.txt 
#time  make dist | tee ../make-dist.txt
