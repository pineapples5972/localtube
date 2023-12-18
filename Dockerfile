FROM python:3.10

WORKDIR /app

COPY . .

RUN pip install -r requirements.txt

RUN . venv/bin/activate
EXPOSE 8080

CMD ["bash", "-c", "python server.py -r"]
