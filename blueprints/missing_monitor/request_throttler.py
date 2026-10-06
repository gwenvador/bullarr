"""
Gestionnaire de throttling pour limiter les requêtes aux sources externes (Prowlarr, etc.)
"""
import time
from typing import Any, Dict, List
from datetime import datetime, timedelta
import threading


class RequestThrottler:
    """Limite le nombre de requêtes vers les sources externes"""
    
    def __init__(self, requests_per_minute: int = 6):
        """
        Initialise le throttler
        
        Args:
            requests_per_minute: Nombre maximal de requêtes par minute
        """
        self.requests_per_minute = requests_per_minute
        self.min_interval = 60.0 / requests_per_minute  # Délai minimum entre requêtes
        self.last_request_time = {}  # Par source
        self.request_queue = {}  # Requêtes en attente par source
        self.lock = threading.Lock()
    
    def wait_if_needed(self, source: str):
        """Attend si nécessaire avant d'effectuer une requête
        
        Args:
            source: Nom de la source (prowlarr, ebdz, etc.)
        """
        with self.lock:
            current_time = time.time()
            
            if source not in self.last_request_time:
                self.last_request_time[source] = 0
            
            last_time = self.last_request_time[source]
            time_since_last = current_time - last_time
            
            if time_since_last < self.min_interval:
                wait_time = self.min_interval - time_since_last
                time.sleep(wait_time)
            
            self.last_request_time[source] = time.time()
    
    


class SearchResultCache:
    """Cache simple pour les résultats de recherche"""
    
    def __init__(self, cache_duration_minutes: int = 60):
        """
        Initialise le cache
        
        Args:
            cache_duration_minutes: Durée de vie du cache en minutes
        """
        self.cache_duration = timedelta(minutes=cache_duration_minutes)
        self.cache = {}  # {cache_key: (results, timestamp)}
        self.lock = threading.Lock()
    
    def generate_key(self, source: str, title: str, volume_num: int, thread_id: int = None, label: str = None) -> str:
        """Génère une clé de cache

        Args:
            source: Source de recherche
            title: Titre de la série
            volume_num: Numéro du volume
            thread_id: Thread EBDZ ciblé, le cas échéant (distingue une intégrale d'un
                tome normal partageant le même numéro)
            label: Texte de recherche alternatif (ex. "Intégrale 6"), le cas échéant

        Returns:
            Clé de cache
        """
        suffix = f":t{thread_id}" if thread_id else ""
        suffix += f":{label.lower()}" if label else ""
        return f"{source}:{title.lower()}:vol{volume_num}{suffix}"
    
    def get(self, key: str) -> Any:
        """Récupère une valeur du cache
        
        Args:
            key: Clé de cache
            
        Returns:
            Résultats ou None si expiré/non trouvé
        """
        with self.lock:
            if key not in self.cache:
                return None
            
            results, timestamp = self.cache[key]
            
            if datetime.now() - timestamp > self.cache_duration:
                del self.cache[key]
                return None
            
            return results
    
    def set(self, key: str, results: List[Dict]):
        """Stocke une valeur dans le cache
        
        Args:
            key: Clé de cache
            results: Résultats à cacher
        """
        with self.lock:
            self.cache[key] = (results, datetime.now())
    
    def clear(self):
        """Vide le cache"""
        with self.lock:
            self.cache.clear()
    
    def stats(self) -> Dict:
        """Retourne des stats sur le cache"""
        with self.lock:
            return {
                'total_entries': len(self.cache),
                'cache_size_bytes': sum(
                    len(str(results)) for results, _ in self.cache.values()
                )
            }


