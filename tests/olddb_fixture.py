"""旧库构造：模拟升级前的原始 schema（外键无 ON DELETE）与历史孤儿。"""
import sqlite3

# 与升级前 models.py 等价的建表语句：外键仅 REFERENCES，无 ON DELETE 动作。
OLD_DDL = """
CREATE TABLE ponds (
  id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(100) NOT NULL UNIQUE,
  area FLOAT NOT NULL, water_depth FLOAT NOT NULL, species VARCHAR(100),
  status VARCHAR(20), created_at DATETIME, updated_at DATETIME);
CREATE TABLE batches (
  id INTEGER NOT NULL PRIMARY KEY, batch_number VARCHAR(50) NOT NULL UNIQUE,
  pond_id INTEGER NOT NULL REFERENCES ponds(id),
  species VARCHAR(100) NOT NULL, stocking_date DATE NOT NULL,
  estimated_harvest_date DATE, actual_harvest_date DATE, status VARCHAR(20),
  created_at DATETIME, updated_at DATETIME);
CREATE TABLE stocking_records (
  id INTEGER NOT NULL PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  species VARCHAR(100) NOT NULL, quantity INTEGER NOT NULL, source VARCHAR(200),
  batch_number VARCHAR(50), weight_per_unit FLOAT, total_weight FLOAT,
  notes TEXT, created_at DATETIME);
CREATE TABLE feeding_records (
  id INTEGER NOT NULL PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  feeding_date DATE NOT NULL, feed_type VARCHAR(100) NOT NULL, feed_quantity FLOAT NOT NULL,
  feeding_time VARCHAR(20), weather VARCHAR(50), water_temperature FLOAT,
  notes TEXT, created_at DATETIME);
CREATE TABLE water_quality_records (
  id INTEGER NOT NULL PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  record_date DATE NOT NULL, record_time VARCHAR(20), water_temperature FLOAT,
  ph_value FLOAT, dissolved_oxygen FLOAT, ammonia_nitrogen FLOAT, nitrite FLOAT,
  transparency FLOAT, notes TEXT, created_at DATETIME);
CREATE TABLE medication_records (
  id INTEGER NOT NULL PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  medication_date DATE NOT NULL, drug_name VARCHAR(200) NOT NULL, drug_type VARCHAR(50),
  dosage FLOAT, dosage_unit VARCHAR(20), administration_method VARCHAR(100),
  purpose VARCHAR(200), manufacturer VARCHAR(200), batch_number VARCHAR(50),
  notes TEXT, created_at DATETIME);
CREATE TABLE cost_records (
  id INTEGER NOT NULL PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  cost_date DATE NOT NULL, cost_type VARCHAR(50) NOT NULL, amount FLOAT NOT NULL,
  description VARCHAR(500), quantity FLOAT, unit VARCHAR(20), unit_price FLOAT,
  notes TEXT, created_at DATETIME);
CREATE TABLE harvest_sales (
  id INTEGER NOT NULL PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  sale_date DATE NOT NULL, weight FLOAT NOT NULL, unit_price FLOAT NOT NULL,
  total_amount FLOAT, buyer VARCHAR(200), batch_number VARCHAR(50),
  quality_grade VARCHAR(50), notes TEXT, created_at DATETIME);
"""


def build_old_db(path: str):
    con = sqlite3.connect(path)
    try:
        con.executescript(OLD_DDL)
        con.execute(
            "INSERT INTO ponds VALUES(1,'P1',10,2,'鲫','active',NULL,NULL)"
        )
        con.execute(
            "INSERT INTO batches VALUES(1,'B-REAL',1,'鲫','2026-03-01',NULL,NULL,'active',NULL,NULL)"
        )
        # 健康明细
        con.execute(
            "INSERT INTO feeding_records(id,batch_id,feeding_date,feed_type,feed_quantity)"
            " VALUES(1,1,'2026-03-02','A',5)"
        )
        # 孤儿但携带现存批次号 B-REAL -> 迁移时应安全补正到 batch 1
        con.execute(
            "INSERT INTO harvest_sales(id,batch_id,sale_date,weight,unit_price,batch_number)"
            " VALUES(10,77,'2026-05-01',3,10,'B-REAL')"
        )
        con.execute(
            "INSERT INTO stocking_records(id,batch_id,species,quantity,batch_number)"
            " VALUES(20,77,'鲫',1000,'B-REAL')"
        )
        # 孤儿且批次号查无此批 -> 隔离待复核
        con.execute(
            "INSERT INTO harvest_sales(id,batch_id,sale_date,weight,unit_price,batch_number)"
            " VALUES(11,88,'2026-05-02',4,10,'B-GONE')"
        )
        # 孤儿且无任何批次号线索 -> 隔离待复核
        con.execute(
            "INSERT INTO feeding_records(id,batch_id,feeding_date,feed_type,feed_quantity)"
            " VALUES(2,99,'2026-03-03','B',6)"
        )
        con.commit()
    finally:
        con.close()
