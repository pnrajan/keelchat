# Image for model-written analysis code (YACHAT_SANDBOX=local with the docker or podman backend).
# Runs with no network, as nobody, with memory and process limits set by the service. Nothing is installed at run time.
FROM python:3.12-slim
RUN pip install --no-cache-dir "pandas>=2.2,<4" "numpy>=2" "scipy>=1.14" "statsmodels>=0.14" "scikit-learn>=1.5" \
    && mkdir -p /sandbox && chmod 1777 /sandbox
WORKDIR /sandbox
USER nobody
