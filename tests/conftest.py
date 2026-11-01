import os, sys, tempfile
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
sys.path.insert(0, APP)
os.environ.setdefault('ACCESS_TOKEN', 'test-access-token')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='asw-test-'))
os.environ.setdefault('FIVECORE_INGEST_TOKEN', 'test-ingest-token')
