make -C tests test-pylint && \
make -C tests test-bandit && \
make -C tests test-unit && \
make -C tests test-format-python && \
make -C tests test-mypy-raw
