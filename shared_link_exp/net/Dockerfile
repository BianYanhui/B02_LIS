FROM alpine
RUN apk add --no-cache iproute2 iperf3 python3
COPY gateway_relay.py /opt/gateway_relay.py
ENTRYPOINT ["python3", "/opt/gateway_relay.py"]
