# The perf job's toolchain. The digest fixes Python and libc, and apt reads
# the Debian snapshot the base image names as its own source, so the digest
# alone fixes every package: the counts move only with a commit here.
FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Each `URIs:` line takes the snapshot URL in the comment above it; a base that names none fails the build.
RUN awk '/^# http:\/\/snapshot\.debian\.org\/archive\//{ snapshot = $2; next } \
         /^URIs:/{ if (snapshot == "") exit 3; sub(/^http:/, "https:", snapshot); print "URIs: " snapshot; snapshot = ""; rewritten++; next } \
         { print } END { if (rewritten < 1) exit 3 }' /etc/apt/sources.list.d/debian.sources > /tmp/debian.sources \
 && mv /tmp/debian.sources /etc/apt/sources.list.d/debian.sources \
 && cat /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=3 update -q \
 && apt-get -o Acquire::Retries=3 install -y -q --no-install-recommends valgrind git \
 && rm -rf /var/lib/apt/lists/* \
 && python -m compileall -q /usr/local/lib/python3.13
# The image ships the stdlib without bytecode; compiled once here, a count
# measures imports as an installed Python makes them, not the compiler.
