# ExportWhy: reproducible build (Technical Execution Guide §3). Zetaris and Nightshift run on the host;
# inside the container point at them with host.docker.internal (see .env.example). SAMPLE_MODE=true needs neither.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .
# the SAMPLE_MODE twin is built at image build time, so a run never generates data
RUN python data/gen_data.py --seed 7 && python data/build_sample.py && mkdir -p /app/logs /app/traces /app/memory

EXPOSE 8000
CMD ["python", "run.py", "--serve"]
