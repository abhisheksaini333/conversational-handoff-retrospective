import time
from integration_demo import request
for attempt in range(60):
    try:
        status=request('/status',service='rasa')
        if not status.get('model_file'):
            raise RuntimeError('Rasa model is not loaded')
        request('/health')
        print('Rasa and coordinator ready')
        break
    except Exception:
        if attempt==59: raise
        time.sleep(2)
