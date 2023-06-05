FROM python:3.11-slim
LABEL authors="Tom Oinn"

RUN pip install paho-mqtt pixelblaze-client pyyaml
ADD bifrost.py .
RUN mkdir config
ADD config/bifrost.yml config/.

CMD ["python", "./bifrost.py"]