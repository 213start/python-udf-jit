ARG CINDERX_BASE_IMAGE
FROM ${CINDERX_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UDFJIT_MODE=off

WORKDIR /opt/python-udf-jit
COPY constraints/scalar-piercing.txt constraints/scalar-piercing.txt

# Daft 0.7.2 only declares its older Ray range in the optional `ray` extra.
# Install Daft itself first, then the explicitly pinned experiment version;
# runtime incompatibility is a STOP rather than a silent downgrade.
RUN unset PYTHONJITAUTO PYTHONJITDISABLE; \
    python -m pip install --no-cache-dir daft==0.7.2 pyarrow==22.0.0 \
    && python -m pip install --no-cache-dir 'ray[client,data,default]==2.55.0'

COPY pyproject.toml README.md ./
COPY src src
COPY config config
COPY tests tests
RUN unset PYTHONJITAUTO PYTHONJITDISABLE; \
    python -m pip install --no-cache-dir --no-deps .

COPY docker/scalar-piercing/python-udf-jit-bootstrap.pth /tmp/python-udf-jit-bootstrap.pth
RUN unset PYTHONJITAUTO PYTHONJITDISABLE; \
    python -c "import shutil,sysconfig; shutil.copyfile('/tmp/python-udf-jit-bootstrap.pth', sysconfig.get_path('purelib') + '/python-udf-jit-bootstrap.pth')"

COPY docker/scalar-piercing/entrypoint.sh /usr/local/bin/scalar-piercing-entrypoint
RUN chmod 0555 /usr/local/bin/scalar-piercing-entrypoint

ENTRYPOINT ["/usr/local/bin/scalar-piercing-entrypoint"]
