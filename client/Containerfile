# Use an NVIDIA base image so XGBoost can talk to the Jetson's GPU
FROM nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3

# Set the working directory inside the container
WORKDIR /app

# Copy your requirements and install them
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy your Python scripts into the container
COPY data-engine.py .
COPY client.py .

# Create the internal buffer directory
RUN mkdir -p /app/ram_buffer