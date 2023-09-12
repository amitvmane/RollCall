FROM python:3.10.0
WORKDIR /app
COPY . .
RUN pip3 install -r Requeriments.txt
WORKDIR /app/rollCall
CMD ["python", "runner.py"]

