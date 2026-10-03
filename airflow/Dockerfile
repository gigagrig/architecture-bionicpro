FROM apache/airflow:2.10.5-python3.12
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir "apache-airflow==2.10.5" -r /requirements.txt
COPY --chown=airflow:root dags /opt/airflow/dags
COPY --chown=airflow:root seed_demo.py /opt/airflow/seed_demo.py
COPY --chown=airflow:root tests /opt/airflow/tests
