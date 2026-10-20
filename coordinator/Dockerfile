FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY coordinator.py relay_transport.py chat_admission.py relay_client.py quic_control.py rpc_handoff.py relay_policy.py rpc_performance.py chat_stream.py .

EXPOSE 8080

CMD ["python", "coordinator.py", "--host", "0.0.0.0", "--port", "8080"]
