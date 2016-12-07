from flask import Flask
# from stream2segment.gui.webapp.core import classannotator
# from flask import url_for
# from flask import request
# app = Flask(__name__, static_folder=None)  # static_folder=None DOES TELL FLASK NOT TO ADD ROUTE
# FOR STATIC FOLDEERS

app = Flask(__name__)
# app.config.from_object('config')

# this has to come AFTER app ABOVE
from stream2segment.gui.webapp import views  # nopep8

# from stream2segment.io.db import ListReader
# from flask import jsonify
# from stream2segment.gui.webapp import core
# from flask import request



# @app.route("/get_elements", methods=['POST'])
# def get_elements():
#     db_uri = app.config['DATABASE_URI']
#     json_req = request.get_json()
#     class_ids = [] if json_req is None else json_req.get('class_ids', [])
#     if class_ids:
#         class_ids = [int(c) for c in class_ids]
#     return jsonify(core.get_ids(db_uri, class_ids))
# 
# 
# @app.route("/get_data", methods=['POST'])
# def get_data():
#     data = request.get_json()
#     seg_id = data['segId']
#     # NOTE: seg_id is a unicode string, but the query to the db works as well
#     db_uri = app.config['DATABASE_URI']
#     return jsonify(core.get_data(db_uri, seg_id))
# 
# 
# @app.route("/set_class_id", methods=['POST'])
# def set_class_id():
#     data = request.get_json()
#     class_id = data['classId']
#     seg_id = data['segmentId']
#     old_class_id = core.set_class(seg_id, class_id)
#     # Flask complains if return is missing. FIXME: check better!
#     return str(old_class_id)

