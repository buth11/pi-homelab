# Build needs the extra named context "smartreport" (the repo's smart-report/
# directory, shared with the CLI):
#   docker buildx build --build-context smartreport=../../smart-report .
# CI passes it via build-contexts in .github/workflows/dashboard.yml.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=smartreport smart_report.py smart_store.py ./
COPY --from=smartreport hosts.conf endurance.conf ./smart/
COPY main.py smart.py ./
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
