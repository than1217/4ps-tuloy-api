import mysql.connector.cursor

class FakeConn:
    def __init__(self):
        self.charset = 'utf8'
    def cmd_query(self, query): pass
    def get_server_info(self): return '8.0'

class MockCursor(mysql.connector.cursor.MySQLCursor):
    def __init__(self):
        self._connection = FakeConn()
    def _handle_result(self, *args): pass
    def execute(self, operation, params=None, multi=False):
        import mysql.connector.conversion
        conv = mysql.connector.conversion.MySQLConverter()
        escaped = tuple(conv.escape(p) if isinstance(p, str) else p for p in params)
        escaped = tuple(f"'{p}'" if isinstance(p, str) else p for p in escaped)
        stmt = operation % escaped
        print('Final Query:', stmt)

c = MockCursor()
query = r"""
    SELECT * FROM vw_verify_member_bene 
    WHERE match_keys = MD5(CONCAT_WS('/', 
        UPPER(TRIM(REGEXP_REPLACE(%s, '\\s+', ' '))),
        UPPER(TRIM(REGEXP_REPLACE(%s, '\\s+', ' '))), 
        %s
    ))
"""
c.execute(query, ('John', 'Doe', '1990-01-01'))
