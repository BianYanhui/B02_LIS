FROM alpine
RUN apk add --no-cache iproute2 iperf3 python3
COPY gateway_relay_4t4.py /opt/gateway_relay_4t4.py
ENTRYPOINT ["python3", "/opt/gateway_relay_4t4.py"]
