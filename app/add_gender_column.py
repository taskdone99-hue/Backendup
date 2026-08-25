import pymysql

conn = pymysql.connect(
    host='database-2.ci9ueyck4xdq.us-east-1.rds.amazonaws.com',
    user='admin',
    password='Anjali916015',
    database='instaapp',
    port=3306
)
cur = conn.cursor()

try:
    cur.execute("ALTER TABLE users ADD COLUMN gender ENUM('male','female','non_binary','prefer_not_to_say') NULL")
    print("users.gender added")
except pymysql.err.OperationalError as e:
    print("users:", e)

try:
    cur.execute("ALTER TABLE pending_signups ADD COLUMN gender ENUM('male','female','non_binary','prefer_not_to_say') NULL")
    print("pending_signups.gender added")
except pymysql.err.OperationalError as e:
    print("pending_signups:", e)

conn.commit()
conn.close()
print("Done")