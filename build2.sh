# need to run "scl enable devtoolset-8 bash" on centos 7 build host
# exit codes
e_ok=0
e_warning=1
e_critical=2
e_unknown=3
status=0

# regular expression that matches queue IDs (e.g. D71EF7AC80F8)

usage="Usage Example: $0 check-mk-raw-1.6.0p27.cre"
usage2="Download: wget https://download.checkmk.com/checkmk/1.6.0.p27/check-mk-raw-1.6.0p27.cre.tar.gz"

if [ -z $1 ]; then
    echo $usage
    echo $usage2
    exit $e_unknown
fi


if [ ! -f ./$1.tar.gz ]; then
    echo "wget https://download.checkmk.com/checkmk/VERSIONB/$1.tar.gz"
    exit $e_unknown
fi



rm -rf $1
tar xzf $1.tar.gz && cd $1
# this line need to be fixed in config file
cp ../$1.tar.gz .
./configure --with-nagios4
time make rpm | tee -a ../make-rpm.txt



#time  make dist | tee -a ../make-dist.txt
# get the livestatus.o of this version
#cp -p ./omd/build/package_build/(basename $1 .cre)/src/livestatus.o ../$1.livestatus.o

