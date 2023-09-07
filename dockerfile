FROM python:3.10.0
WORKDIR /app
COPY . .
RUN pip3 install -r requeriments.txt
CMD ["python", "rollCall/runner.py"]

