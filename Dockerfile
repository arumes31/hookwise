# Use an official Python runtime as a parent image
FROM python:3.14-slim

# Set the working directory in the container
WORKDIR /app

# Create a non-root user and switch to it
RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser /app
USER appuser

# Copy the requirements file into the container at /app
COPY --chown=appuser:appuser requirements.txt .

# Install any needed packages specified in requirements.txt
# User install is safer, add ~/.local/bin to PATH
RUN pip install --no-cache-dir --user -r requirements.txt
ENV PATH="/home/appuser/.local/bin:${PATH}"

# Copy the rest of the working directory contents into the container at /app
COPY --chown=appuser:appuser . .

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Run app.py when the container launches using Gunicorn with eventlet for WebSockets
CMD ["gunicorn", "--worker-class", "eventlet", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]
