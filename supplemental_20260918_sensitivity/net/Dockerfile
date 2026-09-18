FROM alpine
RUN apk add --no-cache iproute2 iperf3 python3
COPY gateway_relay_sens.py /opt/gateway_relay_sens.py
ENTRYPOINT ["python3", "/opt/gateway_relay_sens.py"]
