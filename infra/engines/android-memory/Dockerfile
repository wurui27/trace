ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}

# Release builds must pass PYTHON_BASE_IMAGE as an externally verified @sha256 reference.
RUN groupadd --system --gid 65532 perfpilot && \
    useradd --system --uid 65532 --gid 65532 --no-create-home perfpilot
WORKDIR /opt/android-memory
COPY --chown=65532:65532 . /opt/android-memory
USER 65532:65532
ENTRYPOINT ["python3", "tools/ai_context.py"]
