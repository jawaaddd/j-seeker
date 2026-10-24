# j-seeker's runtime: Python and its dependencies. The code itself is mounted at /app by
# docker-compose.yml, so edits take effect without a rebuild (rebuild after changing requirements.txt).
# Python 3.14: the current Essentia release only ships wheels for this.
FROM python:3.14-slim

# libatomic1: Essentia's wheel links against it, and the slim image leaves it out.
# tzdata-legacy: zone names like US/Eastern, which hosts may pass in as TZ.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 tzdata-legacy && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "jseeker"]
