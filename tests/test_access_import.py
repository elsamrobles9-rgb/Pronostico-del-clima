import io,tempfile,time,unittest
from app import create_app
from contract import dumps
from services.endpoints import DemoClient,sample
from services.imports import parse_file
class AccessImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.backend=DemoClient();self.app=create_app(self.backend,auth_path=self.temp.name+'/auth.sqlite3');self.app.testing=True
        self.web=self.app.test_client();self.store=self.app.extensions['access_store']
        self.store.add_user('admin','correct-password-123','ADMIN')
    def csrf(self):
        with self.web.session_transaction() as s:return s['csrf']
    def login(self,name='admin'):
        self.web.get('/login')
        return self.web.post('/login',data={'csrf':self.csrf(),'username':name,'password':'correct-password-123'})
    def upload(self,rows):
        return self.web.post('/import',data={'csrf':self.csrf(),'file':(io.BytesIO(dumps(rows).encode()),'records.json')})
    def test_login_and_csrf(self):
        self.assertEqual(self.web.get('/').status_code,302)
        self.assertEqual(self.web.get('/health').status_code,200)
        self.assertEqual(self.web.post('/login',data={}).status_code,400)
        self.assertEqual(self.login().status_code,302)
        self.assertEqual(self.web.get('/').status_code,200)
    def test_expiry_and_revocation(self):
        self.login()
        with self.store.connect() as db:db.execute('UPDATE sessions SET touched=?',(time.time()-1801,))
        self.assertEqual(self.web.get('/').status_code,302)
        self.login();self.store.disable('admin');self.assertEqual(self.web.get('/').status_code,302)
    def test_analyst(self):
        self.store.add_user('reader','correct-password-123','ANALYST');self.login('reader')
        for route in ['/add','/edit','/delete','/import']:self.assertEqual(self.web.get(route).status_code,403)
        self.assertNotIn('Add record',self.web.get('/').get_data(as_text=True))
    def test_throttle_and_logout(self):
        for i in range(5):self.assertIsNone(self.store.login('admin','wrong'))
        self.assertIsNone(self.store.login('admin','correct-password-123'))
        with self.store.connect() as db:db.execute('DELETE FROM attempts')
        self.login();self.web.post('/logout',data={'csrf':self.csrf()});self.assertEqual(self.web.get('/').status_code,302)
    def test_preview_confirm_replay(self):
        self.login();r=self.upload([{**sample(),'location_id':'IMPORT-NEW'}]);self.assertEqual(r.status_code,302)
        self.assertEqual(len(self.backend.table.rows),1)
        self.assertEqual(self.web.get(r.location).status_code,200)
        for i in range(2):self.web.post(r.location+'/confirm',data={'csrf':self.csrf()})
        self.assertEqual(len(self.backend.table.rows),2)
        self.assertIn('Imported',self.web.get(r.location).get_data(as_text=True))
    def test_invalid_file_blocks_writes(self):
        self.login();r=self.upload([{**sample(),'humidity_pct':101}])
        self.assertEqual(self.web.post(r.location+'/confirm',data={'csrf':self.csrf()}).status_code,400)
        self.assertEqual(len(self.backend.table.rows),1)
    def test_existing_not_overwritten(self):
        self.login();r=self.upload([{**sample(),'uv_index':9}]);self.web.post(r.location+'/confirm',data={'csrf':self.csrf()})
        self.assertEqual(next(iter(self.backend.table.rows.values()))['version'],1)
        self.assertIn('Not confirmed',self.web.get(r.location).get_data(as_text=True))
    def test_owner_and_csrf(self):
        self.login();r=self.upload([sample()]);self.assertEqual(self.web.post(r.location+'/confirm',data={}).status_code,400)
        self.store.add_user('other','correct-password-123','ADMIN');self.login('other')
        self.assertEqual(self.web.get(r.location).status_code,404)
        self.assertEqual(self.web.post(r.location+'/confirm',data={'csrf':self.csrf()}).status_code,404)
    def test_limits_and_duplicates(self):
        for filename,raw in [('a.exe',b'[]'),('a.json',b'[]'),('a.json',b'{"x":1,"x":2}'),('a.json',b'x'*262145),('a.json',dumps([sample()]*51).encode())]:
            with self.assertRaises(ValueError):parse_file(filename,raw)
        self.assertTrue(parse_file('a.json',dumps([sample(),sample()]).encode())[1]['error'])
    def test_template_roundtrip(self):
        self.login()
        for kind in ('csv','json'):
            response=self.web.get('/import/template/'+kind);self.assertEqual(response.status_code,200)
            self.assertFalse(parse_file('sample.'+kind,response.data)[0]['error'])
