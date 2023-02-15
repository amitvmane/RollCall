from pymongo import MongoClient
from pymongo import ASCENDING, DESCENDING
from matplotlib import pyplot as plt
from matplotlib import dates as mdates
from datetime import datetime, timedelta
import io
from collections import Counter
import numpy as np
import calendar

from models.Database import db

db = db.db

def get_data():
    # Seleccionar la base de datos y la colección
    collection = db["chats"]

    # Obtener el primer y último día con chats
    first_day = collection.find().sort("createdAt", ASCENDING).limit(1)[0]["createdAt"]
    last_day = collection.find().sort("createdAt", DESCENDING).limit(1)[0]["createdAt"]

    # Generar una serie de fechas que cubra todo el rango
    dates = [datetime.strptime((first_day + timedelta(days=x)).strftime("%Y-%m-%d"), "%Y-%m-%d") for x in range((last_day-first_day).days + 1)]

    # Agrupar los documentos por fecha
    pipeline = [
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$createdAt"}}, "count": {"$sum": 1}}}
    ]
    result = list(collection.aggregate(pipeline))
    
  
    # Ordenar los resultados por fecha
    result = sorted(result, key=lambda x: x["_id"])

    # Convertir los resultados a un diccionario con fechas como claves
    result_dict = {datetime.strptime(item["_id"], '%Y-%m-%d'): item["count"] for item in result}
  
    # Rellenar los valores faltantes con ceros
    counts = np.cumsum([result_dict.get(date, 0) for date in dates])

    # Crear un gráfico de líneas con la cantidad de chats por fecha
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.gca().xaxis.set_major_locator(mdates.DayLocator())

    fig = plt.figure()
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.gca().xaxis.set_major_locator(mdates.DayLocator())
    plt.plot(dates, counts, color="blue")
    plt.xlabel("Date")
    plt.ylabel("Registered chats")
    plt.title("Chart of Number of Chats by Date")
    plt.gcf().autofmt_xdate()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)

    return buf

def get_users():
    collection = db["users"]

    result = next(collection.aggregate([
        {"$unwind": "$users"},
        {"$group": {
            "_id": '$users.registeredAt',
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]))

    if not isinstance(result, list):
        result = [result, {"_id": datetime.now() + timedelta(days=1), "count":1}, {"_id": datetime.now() + timedelta(days=2), "count":3}]

    # Obtiene la fecha de registro de cada usuario único
    dates = [doc["_id"].date() for doc in result]
    counts = np.cumsum([doc['count'] for doc in result])

    # Crea el gráfico
    fig = plt.figure()
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.gca().xaxis.set_major_locator(mdates.DayLocator())
    plt.plot(dates, counts, color="blue")
    plt.xlabel("Date")
    plt.ylabel("Users registered")
    plt.title("Graph of Number of Registered Users by Date")
    plt.gcf().autofmt_xdate()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)

    return buf

def total_downtime_or_uptime():
    result = db['system'].find({"_id":'63d4cc4378e1ae99cb9f329e'}, {"last_login":1, 'last_logout':1})
    return next(result)

def active_rollcalls():

    pipeline = [
    {"$unwind": "$rollCalls"},
    {"$group": {"_id": None, "total_elements": {"$sum": 1}}}
    ]

    result = db['rollCalls'].aggregate(pipeline)

    return next(result)['total_elements']

def get_chat_zones():
    pipeline = [
    {"$group": {"_id": "$config.timezone", "count": {"$sum": 1}}},
    {"$sort": {"count": -1}}
    ]
    result = list(db['chats'].aggregate(pipeline))
    
    ids = [item['_id'] for item in result]
    counts = [item['count'] for item in result]

    fig = plt.figure() 
    plt.bar(ids, counts)
    plt.xlabel("Timezone")
    plt.ylabel("Number of documents")
    plt.title("Number of documents per timezone")

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)

    return buf

def get_all_rollcalls():
    pipeline = [
    {"$unwind": "$rollCalls"},
    {"$group": {"_id": None, "total_elements": {"$sum": 1}}}
    ]

    active = db['rollCalls'].aggregate(pipeline)

    pipeline = [
    {"$unwind": "$endedRollCalls"},
    {"$group": {"_id": None, "total_elements": {"$sum": 1}}}
    ]

    ended = db['endedRollCalls'].aggregate(pipeline)

    return next(active)['total_elements'] + next(ended)['total_elements']

def get_users_voting(users, cid):
    try:
        resultActive = db['rollCalls'].aggregate([
            {
                "$match": {
                    "_id": cid
                }
            },
            {
                "$unwind": "$rollCalls"
            },
            {
                "$project": {
                    "_id": 0,
                    "total_attendees": { "$concatArrays": [ "$rollCalls.inList", "$rollCalls.outList", "$rollCalls.maybeList", "$rollCalls.waitList" ] }
                }
            }
        ])

        resultEnded = db['endedRollCalls'].aggregate([
            {
                "$match": {
                    "_id": cid
                }
            },
            {
                "$unwind": "$endedRollCalls"
            },
            {
                "$project": {
                    "_id": 0,
                    "total_attendees": { "$concatArrays": [ "$endedRollCalls.inList", "$endedRollCalls.outList", "$endedRollCalls.maybeList", "$endedRollCalls.waitList" ] }
                }
            }
        ])

        totalResult = next(resultActive)['total_attendees'] + next(resultEnded)['total_attendees']
        totalUsers = len(set(user['user_id'] for user in totalResult if type(user['user_id'])==int))
        
        return (totalUsers/(users - 1)) * 100
        
    except Exception as e:
        print(e)

def get_users_voting_in(cid):
    result = next(db['endedRollCalls'].aggregate([
        {
            "$match": {
                "_id": cid
            }
        },
        {"$unwind": "$endedRollCalls"},
        {"$unwind": "$endedRollCalls.inList"},
        {
            "$group": {
            "_id": "$endedRollCalls.inList.name",
            "count": {"$sum": 1}
            }
        },
        {"$sort": {"count":-1}},
        {"$limit": 5}
        
    ]))

    if not isinstance(result, list):
        result = [result]

    names = [user['_id'] for user in result]
    count = [user['count'] for user in result]

    fig = plt.figure()
    plt.bar(names, count)
    plt.xlabel("User")
    plt.ylabel("Number of changes")
    plt.title("Top 5 users with more changes from IN to OUT")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)

    return buf

def users_changing_from_in_to_out(cid):
    result = next(db['users'].aggregate([{"$match":{"_id":cid}},{"$unwind":"$users"}, {"$group":{"_id":"$users.name", "count":{"$sum":"$users.in_to_out_count"}}}, {"$sort": {"count": -1}}, {"$limit":5}]))

    if not isinstance(result, list):
        result = [result]

    users = [user['_id'] for user in result]
    counts = [user['count'] for user in result]

    fig = plt.figure()
    plt.bar(users, counts)
    plt.xlabel("User")
    plt.ylabel("Number of assists")
    plt.title("Top 5 users with more assists")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)

    return buf

def users_consistently_responding(cid):
    result = next(db['users'].aggregate([{"$match":{"_id":cid}},{"$unwind":"$users"}, {"$group":{"_id":"$users.name", "count":{"$sum":"$users.responses"}}}, {"$sort": {"count": -1}}, {"$limit":5}]))

    if not isinstance(result, list):
        result = [result]

    users = [user['_id'] for user in result]
    counts = [user['count'] for user in result]

    fig = plt.figure()
    plt.bar(users, counts)
    plt.xlabel("User")
    plt.ylabel("Number of responses")
    plt.title("Top 5 users with more responses (in/out/maybe)")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)

    return buf

def rollcalls_month(cid):
    result = next(db["endedRollCalls"].aggregate([{"$match":{"_id":cid}}, {"$unwind":"$endedRollCalls"},{
        "$group": {
            "_id": {
                "$month": "$endedRollCalls.createdDate"
            },
            "count": {
                "$sum": 1
            }
        }
    }]))

    if not isinstance(result, list):
        result = [result]

    months = [calendar.month_name[doc['_id']] for doc in result]
    counts = [doc['count'] for doc in result]

    fig = plt.figure()
    plt.bar(months, counts)
    plt.xlabel("Month")
    plt.ylabel("Number of rollcalls")
    plt.title("Rollcalls per month")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)

    return buf
    

''' % users voting in the bot ? LISTO
Top 5 available user ( gives IN vote ) by order in last 3/6 months.LISTO
Top 5 Users which are not using bot NOT POSSIBLE
Top 5 users who are changing vote from IN to OUT in same roll call LISTO
Top 5 users who are consistently responding to bot ( no matter if its IN/OUT/MAYBE ) LISTO
Bot usage frequency per month ( total no of rollcalls per month ) LISTO
'''