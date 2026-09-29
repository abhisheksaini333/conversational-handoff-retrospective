"""Create local-only random demo credentials without printing their values."""
import os
import secrets
from pathlib import Path
p=Path('.env')
if p.exists():
    print('.env already exists; preserved')
else:
    fd=os.open(str(p),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        f.write('HANDOFF_TOKEN='+secrets.token_urlsafe(32)+'\nRASA_TOKEN='+secrets.token_urlsafe(32)+'\n')
    print('Created private .env with local demo credentials')
