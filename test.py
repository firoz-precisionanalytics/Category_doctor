
from flask import Flask
import pickle 

app = Flask(__name__)

@app.route("/ping", methods=["GET"])
def ping():
    return "Firoz!"

