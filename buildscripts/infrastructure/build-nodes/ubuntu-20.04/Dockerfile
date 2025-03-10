ARG IMAGE_UBUNTU_20_04
# hadolint ignore=DL3006
# Create base image
FROM ${IMAGE_UBUNTU_20_04} as base

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8 PATH="/opt/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y \
    dialog \
    gdebi \
    git \
    graphviz \
    libenchant1c2a \
    libkrb5-dev \
    libldap2-dev \
    libmysqlclient-dev \
    librrd-dev \
    libsasl2-dev \
    libssl-dev \
    libxml2 \
    make \
    nullmailer \
    php7.4-common \
    python-dev \
    python3-pip \
    rrdtool \
    strace \
    sudo \
    vim \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y \
    apache2 \
    apache2-dev \
    autoconf \
    build-essential \
    bison \
    curl \
    default-jdk \
    devscripts \
    dnsutils \
    dpatch \
    flex \
    gawk \
    gettext \
    git-buildpackage \
    gtk-doc-tools \
    golang-go \
    iputils-ping \
    joe \
    libcurl4-openssl-dev \
    libevent-dev \
    libffi-dev \
    libfreeradius-dev \
    libgd-dev \
    libglib2.0-dev \
    libgnutls28-dev \
    libgsf-1-dev \
    libkrb5-dev \
    libldap2-dev \
    libltdl-dev \
    libmcrypt-dev \
    libmysqlclient-dev \
    libncurses5-dev \
    libpango1.0-dev \
    libpcap-dev \
    libperl-dev \
    libpq-dev \
    libreadline-dev \
    libsqlite3-dev \
    libssl-dev \
    libtool \
    libxml2-dev \
    libxmlsec1-dev \
    openssh-client \
    patch \
    rpcbind \
    rsync \
    smbclient \
    software-properties-common \
    texinfo \
    tk-dev \
    uuid-dev \
    vim \
    libc6-dbg \
    upx-ucl \
    msitools \
    && rm -rf /var/lib/apt/lists/*


# Install our standard tool chain for building in seperate container
# - gnu-toolchain is needed for compiling all the C++ stuff
# - cmake is needed for e.g. building re2
# - openssl is needed by Python 3.7+
# - python is needed by our build / test chain
FROM base as builder
ARG NEXUS_ARCHIVES_URL
ARG NEXUS_USERNAME
ARG NEXUS_PASSWORD
ARG DISTRO
ARG BRANCH_VERSION
ENV NEXUS_ARCHIVES_URL="$NEXUS_ARCHIVES_URL" NEXUS_USERNAME="$NEXUS_USERNAME" NEXUS_PASSWORD="$NEXUS_PASSWORD" DISTRO="$DISTRO" BRANCH_VERSION="$BRANCH_VERSION"

COPY scripts/* /opt/
RUN /opt/install-gnu-toolchain.sh
RUN /opt/install-valgrind.sh
RUN /opt/install-cmake.sh
RUN /opt/install-protobuf-cpp.sh
RUN /opt/install-openssl.sh
RUN /opt/install-python.sh
RUN /opt/install-freetds.sh
RUN /opt/install-rust-cargo.sh

# Now shrink all the binaries and libraries we produced to build a small image
# in the next step
RUN /opt/strip_binaries /opt

# Run this AFTER strip_binaries!!
RUN /opt/install-bazel.sh

### Actual Image ###
FROM base

# Copy our standard tool chain for building
COPY --from=builder /opt /opt
ARG DISTRO
ARG BRANCH_VERSION
ENV DISTRO="$DISTRO" BRANCH_VERSION="$BRANCH_VERSION"

# Set symlinks
RUN /opt/install-gnu-toolchain.sh link-only
RUN /opt/install-valgrind.sh link-only
RUN /opt/install-cmake.sh link-only
RUN /opt/install-protobuf-cpp.sh --link-only
RUN /opt/install-python.sh link-only
RUN /opt/install-freetds.sh link-only
RUN /opt/install-bazel.sh link-only

# Exclusively for this distro at the moment, because it is used by several CI
# jobs for special tasks (checking out git, "make dist", orchestrating other
# containers, ...). Scripts should be usable with other Ubuntu versions.
RUN /opt/install-docker.sh
RUN /opt/install-make-dist-deps.sh
RUN /opt/install-nodejs.sh
RUN /opt/install-clang.sh
RUN /opt/install-shellcheck.sh
RUN /opt/install-rust-cargo.sh link-only
RUN /opt/install-musl-tools.sh

# Install non cached dependencies
RUN /opt/install-pipenv.sh
RUN /opt/install-cmk-dependencies.sh
RUN /opt/install-patchelf.sh
RUN /opt/install-packer.sh
RUN /opt/install-aws-cli.sh
RUN /opt/install-buildifier.sh

# Ensure all our build containers have the jenkins user (with same uid/gid). The non privileged
# jobs will be executed as this user in the container
RUN groupadd -g 1000 jenkins \
    && useradd -m -u 1001 -g 1000 -s /bin/bash jenkins

RUN mkdir -p /home/jenkins/.cache/ \
    && chown jenkins:jenkins /home/jenkins/.cache/

ARG VERS_TAG
RUN echo $VERS_TAG > /version.txt

ENV RUSTUP_HOME=/opt/rust/rustup
LABEL \
    com.tribe29.image_type="build-image"
