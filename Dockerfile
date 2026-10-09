FROM python:3.12-slim
WORKDIR /category-doctor 

RUN python3 -m pip install --upgrade pip
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

COPY . .

CMD ["python3", "-m", "flask","--app","test.py", "run", "--host=0.0.0.0"]

