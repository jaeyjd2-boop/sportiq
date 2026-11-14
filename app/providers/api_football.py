import httpx
from app.config import settings
class ApiFootballError(RuntimeError): pass
class ApiFootballClient:
    def __init__(self):
        if not settings.api_football_key: raise ApiFootballError("API_FOOTBALL_KEY is not configured")
        self.client=httpx.Client(base_url=settings.api_football_base_url.rstrip("/"),headers={"x-apisports-key":settings.api_football_key},timeout=settings.api_football_timeout)
    def get(self,endpoint,params=None):
        r=self.client.get(endpoint,params=params or {}); r.raise_for_status(); data=r.json()
        if data.get("errors"): raise ApiFootballError(str(data["errors"]))
        return data.get("response",[])
    def leagues(self): return self.get("/leagues")
    def fixtures(self,day): return self.get("/fixtures",{"date":day})
    def live(self): return self.get("/fixtures",{"live":"all"})
    def close(self): self.client.close()
