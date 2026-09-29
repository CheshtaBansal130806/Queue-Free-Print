(function () {
  let map = null, geocoder = null, userMarker = null, selectedMarker = null, directionsService = null, directionsRenderer = null;
  let mapsReady = null, shops = [], currentUser = null, locationWatchId = null, activeDestination = null, lastRerouteAt = 0, routeRequestInFlight = false;

  async function fetchJson(url, options) {
    const r = await fetch(url, options || {});
    const text = await r.text();
    const type = r.headers.get('content-type') || '';
    if (!type.includes('application/json')) {
      throw new Error(r.status === 401 || r.status === 403 ? 'Please log in again and try again.' : 'Server returned an unexpected page. Please restart the Flask app and try again.');
    }
    let data;
    try { data = JSON.parse(text); } catch (_) { throw new Error('Server returned invalid JSON.'); }
    if (!r.ok) throw new Error(data.error || 'Request failed.');
    return data;
  }

  function loadGoogleMaps() {
    if (mapsReady) return mapsReady;
    mapsReady = fetchJson('/api/maps-config', {credentials:'same-origin', cache:'no-store'})
      .then(cfg => new Promise((resolve, reject) => {
        if (!cfg.api_key) return reject(new Error('Google Maps API key is not configured.'));
        if (window.google && window.google.maps) return resolve();
        window.__qfpMapsLoaded = () => resolve();
        const script = document.createElement('script');
        script.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(cfg.api_key) + '&callback=__qfpMapsLoaded';
        script.async = true; script.defer = true;
        script.onerror = () => reject(new Error('Google Maps could not be loaded.'));
        document.head.appendChild(script);
      }));
    return mapsReady;
  }

  function ensureModal() {
    if (document.getElementById('qfpNearbyModal')) return document.getElementById('qfpNearbyModal');
    const modal = document.createElement('div');
    modal.id = 'qfpNearbyModal'; modal.className = 'qfp-nearby-modal';
    modal.innerHTML = `
      <div class="qfp-nearby-shell">
        <div class="qfp-nearby-head"><div><h2 id="qfpNearbyTitle">Nearby QueueFree Shops</h2><p id="qfpNearbySub">Registered shops within 1 km</p></div><button class="qfp-nearby-close" onclick="qfpCloseNearbyShops()">×</button></div>
        <div class="qfp-nearby-body"><div id="qfpNearbyMap" class="qfp-nearby-map"></div><aside id="qfpNearbyPanel" class="qfp-nearby-panel"><div class="qfp-nearby-loading">Finding nearby shops…</div></aside></div>
      </div>`;
    document.body.appendChild(modal);
    return modal;
  }

  function renderPanel() {
    const panel = document.getElementById('qfpNearbyPanel');
    if (!panel) return;
    if (!shops.length) { panel.innerHTML = '<div class="qfp-empty-shops"><strong>No QueueFree shops within 1 km</strong><p>Try again from a different location.</p></div>'; return; }
    panel.innerHTML = `<div class="qfp-shop-list-title">${shops.length} registered shop${shops.length>1?'s':''} nearby</div>` + shops.map((s,i)=>`
      <button class="qfp-shop-item" onclick="qfpSelectShop(${i})"><div class="qfp-shop-top"><strong>${esc(s.shop_name)}</strong><span>${Number(s.distance_km).toFixed(2)} km</span></div><div class="qfp-shop-meta">${s.is_open ? '<span class="qfp-open">● Open</span>' : '<span class="qfp-closed">● Closed</span>'} · ${s.rating ? '★ '+Number(s.rating).toFixed(1) : 'No rating'} · ${s.review_count||0} reviews</div><div class="qfp-shop-id">${esc(s.shop_number || '')}</div></button>`).join('');
  }
  function esc(v){ return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

  window.qfpSelectShop = function(i) {
    if(directionsRenderer) directionsRenderer.set('directions',null);
    const s=shops[i]; if(!s || !map) return;
    map.panTo({lat:s.latitude,lng:s.longitude}); map.setZoom(17);
    if(selectedMarker) selectedMarker.setMap(null);
    selectedMarker=new google.maps.Marker({position:{lat:s.latitude,lng:s.longitude},map,title:s.shop_name,animation:google.maps.Animation.DROP});
    const reviews=(s.reviews||[]).map(x=>`<div class="qfp-review">“${esc(x)}”</div>`).join('');
    document.getElementById('qfpNearbyPanel').innerHTML=`<button class="qfp-back-shops" onclick="qfpRenderShopList()">← All nearby shops</button><div class="qfp-detail-card"><h3>${esc(s.shop_name)}</h3><div class="qfp-shop-id">${esc(s.shop_number||'')}</div><div class="qfp-status ${s.is_open?'is-open':'is-closed'}">${s.is_open?'● Open':'● Closed'}</div><div class="qfp-rating">${s.rating?'★ '+Number(s.rating).toFixed(1):'No rating yet'} <span>(${s.review_count||0} reviews)</span></div><div class="qfp-distance">${Number(s.distance_km).toFixed(2)} km away</div><p>${esc(s.address||s.university_name||'Registered QueueFree print shop')}</p><button type="button" class="qfp-direction-btn" onclick="qfpGetDirections(${Number(s.latitude)},${Number(s.longitude)})">🧭 Get Directions</button>${reviews?'<div class="qfp-reviews"><h4>Recent Reviews</h4>'+reviews+'</div>':'<div class="qfp-no-review">No reviews yet.</div>'}</div>`;
  };
  window.qfpRenderShopList=function(){ activeDestination=null; renderPanel(); if(selectedMarker){selectedMarker.setMap(null);selectedMarker=null;} if(window.qfpRoutePolyline){window.qfpRoutePolyline.setMap(null);window.qfpRoutePolyline=null;} };
  async function calculateLiveRoute() {
    if (!map || !activeDestination || !currentUser || routeRequestInFlight) return;
    const now=Date.now();
    if(now-lastRerouteAt < 8000) return;
    routeRequestInFlight=true;
    try {
      const origin=currentUser;
      const destination=activeDestination;
      const url='https://router.project-osrm.org/route/v1/driving/'+origin.lng+','+origin.lat+';'+destination.lng+','+destination.lat+'?overview=full&geometries=geojson&steps=true';
      const response=await fetch(url,{cache:'no-store'});
      const data=await response.json();
      if(!response.ok || data.code!=='Ok' || !data.routes || !data.routes[0]) throw new Error('No drivable route was found from your current location.');
      const route=data.routes[0];
      if(window.qfpRoutePolyline) window.qfpRoutePolyline.setMap(null);
      window.qfpRoutePolyline=new google.maps.Polyline({path:route.geometry.coordinates.map(c=>({lat:c[1],lng:c[0]})),geodesic:true,strokeOpacity:0.9,strokeWeight:5,map:map});
      const bounds=new google.maps.LatLngBounds();
      route.geometry.coordinates.forEach(c=>bounds.extend({lat:c[1],lng:c[0]}));
      map.fitBounds(bounds,{top:50,right:30,bottom:50,left:30});
      const distanceKm=(route.distance/1000).toFixed(2);
      const mins=Math.max(1,Math.round(route.duration/60));
      const durationText=mins>=60 ? Math.floor(mins/60)+' hr '+(mins%60)+' min' : mins+' min';
      const steps=(route.legs||[]).flatMap(l=>l.steps||[]).slice(0,30);
      const stepHtml=steps.length ? steps.map((step,i)=>{
        const name=(step.name||'Follow the road').trim();
        const m=Math.max(1,Math.round((step.duration||0)/60));
        return '<div class="qfp-route-step"><span>'+ (i+1) +'</span><div>'+esc(name)+'<small>'+m+' min</small></div></div>';
      }).join('') : '<div class="qfp-no-review">Follow the route shown on the map.</div>';
      const panel=document.getElementById('qfpNearbyPanel');
      if(panel) panel.innerHTML='<button class="qfp-back-shops" onclick="qfpRenderShopList()">← All nearby shops</button><div class="qfp-detail-card qfp-directions-card"><h3>🧭 Live Directions</h3><div class="qfp-direction-summary"><strong>'+distanceKm+' km</strong><span>•</span><strong>'+durationText+'</strong></div><p>Route updates automatically as you move.</p><div class="qfp-live-status">● Live location active</div><button type="button" class="qfp-direction-btn" onclick="qfpClearDirections()">✕ Stop Directions</button><div class="qfp-route-steps"><h4>Route Steps</h4>'+stepHtml+'</div></div>';
      lastRerouteAt=Date.now();
    } catch(e) {
      const panel=document.getElementById('qfpNearbyPanel');
      if(panel) panel.innerHTML='<div class="qfp-map-error"><strong>Could not update directions</strong><p>'+esc(e.message||'A driving route could not be calculated for this location.')+'</p><button class="qfp-save-location" onclick="qfpRenderShopList()">Back to Shops</button></div>';
    } finally { routeRequestInFlight=false; }
  }

  function startLiveLocation() {
    if(locationWatchId!==null || !navigator.geolocation) return;
    locationWatchId=navigator.geolocation.watchPosition(pos=>{
      const next={lat:pos.coords.latitude,lng:pos.coords.longitude};
      currentUser=next;
      if(userMarker) userMarker.setPosition(next);
      if(activeDestination) calculateLiveRoute();
    },()=>{}, {enableHighAccuracy:true,maximumAge:5000,timeout:15000});
  }
  function stopLiveLocation() {
    if(locationWatchId!==null){ navigator.geolocation.clearWatch(locationWatchId); locationWatchId=null; }
  }

  window.qfpGetDirections=async function(lat,lng){
    if(!map || !Number.isFinite(Number(lat)) || !Number.isFinite(Number(lng))) return;
    if(!currentUser || !Number.isFinite(Number(currentUser.lat)) || !Number.isFinite(Number(currentUser.lng))){
      const panel=document.getElementById('qfpNearbyPanel');
      if(panel) panel.innerHTML='<div class="qfp-map-error"><strong>Location required</strong><p>Your current location is needed to show directions.</p><button class="qfp-save-location" onclick="qfpOpenNearbyShops()">Use My Location</button></div>';
      return;
    }
    activeDestination={lat:Number(lat),lng:Number(lng)};
    lastRerouteAt=0;
    startLiveLocation();
    await calculateLiveRoute();
  };
  window.qfpClearDirections=function(){
    activeDestination=null; lastRerouteAt=0;
    if(directionsRenderer) directionsRenderer.set('directions',null);
    if(window.qfpRoutePolyline){window.qfpRoutePolyline.setMap(null);window.qfpRoutePolyline=null;}
    if(shops.length) qfpRenderShopList();
  };
  window.qfpCloseNearbyShops=function(){ activeDestination=null; stopLiveLocation(); const m=document.getElementById('qfpNearbyModal'); if(m)m.classList.remove('open'); document.body.classList.remove('qfp-modal-open'); };

  async function openNearby() {
    if(directionsRenderer) directionsRenderer.set('directions',null);
    if(window.qfpRoutePolyline){window.qfpRoutePolyline.setMap(null);window.qfpRoutePolyline=null;}
    const modal=ensureModal(); modal.classList.add('open'); document.body.classList.add('qfp-modal-open');
    const mapEl=document.getElementById('qfpNearbyMap');
    document.getElementById('qfpNearbyPanel').innerHTML='<div class="qfp-nearby-loading">Loading Google Maps…</div>';
    try {
      await loadGoogleMaps();
      if(!navigator.geolocation) throw new Error('Location is not supported by this browser.');
      const pos=await new Promise((res,rej)=>navigator.geolocation.getCurrentPosition(res,rej,{enableHighAccuracy:true,timeout:12000,maximumAge:60000}));
      currentUser={lat:pos.coords.latitude,lng:pos.coords.longitude};
      map=new google.maps.Map(mapEl,{center:currentUser,zoom:15,mapTypeControl:false,streetViewControl:false,fullscreenControl:false});
      userMarker=new google.maps.Marker({position:currentUser,map,title:'Your location'});
      startLiveLocation();
      document.getElementById('qfpNearbyPanel').innerHTML='<div class="qfp-nearby-loading">Finding registered shops within 1 km…</div>';
      const data=await fetchJson('/api/nearby-shops?lat='+encodeURIComponent(currentUser.lat)+'&lng='+encodeURIComponent(currentUser.lng),{credentials:'same-origin',cache:'no-store'});
      if(!data.success) throw new Error(data.error||'Could not load nearby shops.'); shops=data.shops||[];
      shops.forEach((s,i)=>new google.maps.Marker({position:{lat:s.latitude,lng:s.longitude},map,title:s.shop_name,label:String(i+1)}));
      renderPanel();
    } catch(e) {
      document.getElementById('qfpNearbyPanel').innerHTML='<div class="qfp-map-error"><strong>Could not open nearby shops</strong><p>'+esc(e.message||'Location permission may be required.')+'</p></div>';
    }
  }

  window.qfpOpenNearbyShops=openNearby;

  window.qfpOpenAdminShopPicker=async function(){
    const modal=ensureModal(); modal.classList.add('open'); document.body.classList.add('qfp-modal-open');
    document.getElementById('qfpNearbyTitle').textContent='Pick Shop Location'; document.getElementById('qfpNearbySub').textContent='Your latest saved location is shown. Drag the marker or click the map to change it.';
    document.getElementById('qfpNearbyPanel').innerHTML='<div class="qfp-nearby-loading">Loading map…</div>';
    try{ await loadGoogleMaps();
      const saved=window.qfpSavedShopLocation && Number.isFinite(Number(window.qfpSavedShopLocation.lat)) && Number.isFinite(Number(window.qfpSavedShopLocation.lng)) ? {lat:Number(window.qfpSavedShopLocation.lat),lng:Number(window.qfpSavedShopLocation.lng)} : null;
      const start=saved || {lat:29.15,lng:75.70};
      map=new google.maps.Map(document.getElementById('qfpNearbyMap'),{center:start,zoom:saved?17:14,mapTypeControl:true,streetViewControl:false,fullscreenControl:false}); geocoder=new google.maps.Geocoder();
      if(selectedMarker) selectedMarker.setMap(null);
      if(saved){
        selectedMarker=new google.maps.Marker({position:saved,map,draggable:true,title:'Saved shop location'});
        selectedMarker.addListener('dragend',ev=>updatePickPanel(ev.latLng.toJSON()));
      }
      const pick=(e)=>{ const p=e.latLng.toJSON(); if(selectedMarker)selectedMarker.setMap(null); selectedMarker=new google.maps.Marker({position:p,map,draggable:true,title:'Shop location'}); updatePickPanel(p); selectedMarker.addListener('dragend',ev=>updatePickPanel(ev.latLng.toJSON())); };
      map.addListener('click',pick);
      document.getElementById('qfpNearbyPanel').innerHTML='<div class="qfp-picker-help"><strong>'+(saved?'Current saved location':'Pick your shop')+'</strong><p>'+(saved?'Your previously saved shop location is displayed on the map. Drag the marker or click a new point to change it.':'Click the exact shop location on the map. You can drag the marker to fine-tune it.')+'</p><div id="qfpPickedAddress">'+(saved?'Loading saved address…':'No location selected.')+'</div><button id="qfpSavePicked" class="qfp-save-location" '+(saved?'':'disabled')+'>'+(saved?'Save Changed Location':'Confirm & Save Location')+'</button></div>';
      document.getElementById('qfpSavePicked').onclick=savePicked;
      if(saved) updatePickPanel(saved);
    }catch(e){document.getElementById('qfpNearbyPanel').innerHTML='<div class="qfp-map-error"><strong>Google Maps unavailable</strong><p>'+esc(e.message)+'</p></div>';}
  };


  function updatePickPanel(p){ const el=document.getElementById('qfpPickedAddress'); const btn=document.getElementById('qfpSavePicked'); if(el)el.textContent='Selected location: '+p.lat.toFixed(6)+', '+p.lng.toFixed(6); if(btn){btn.disabled=false;btn.dataset.lat=p.lat;btn.dataset.lng=p.lng;} if(geocoder)geocoder.geocode({location:p},(res,status)=>{if(status==='OK'&&res[0]&&el)el.textContent='Selected: '+res[0].formatted_address;}); }
  async function savePicked(){ const b=document.getElementById('qfpSavePicked'); if(!b)return; b.disabled=true; const fd=new FormData(); fd.append('latitude',b.dataset.lat); fd.append('longitude',b.dataset.lng); try{const d=await fetchJson('/admin/shop-location',{method:'POST',body:fd,credentials:'same-origin'}); if(!d.success)throw new Error(d.error); window.qfpSavedShopLocation={lat:Number(d.latitude),lng:Number(d.longitude)}; updateAdminSavedLocation(window.qfpSavedShopLocation); document.getElementById('qfpNearbyPanel').innerHTML='<div class="qfp-save-success"><strong>Location updated</strong><p>The latest shop location is now saved and visible in Settings.</p><button class="qfp-save-location" onclick="qfpCloseNearbyShops()">Done</button></div>'; }catch(e){b.disabled=false;alert(e.message||'Could not save location.');} }
  function updateAdminSavedLocation(p){
    const status=document.getElementById('qfpSavedLocationStatus'), detail=document.getElementById('qfpSavedLocationDetail');
    if(status) status.textContent='Location saved';
    if(detail) detail.textContent='Latest saved shop location: '+Number(p.lat).toFixed(6)+', '+Number(p.lng).toFixed(6)+'. You can change it anytime using Pick on Map.';
  }

  document.addEventListener('keydown',e=>{if(e.key==='Escape')qfpCloseNearbyShops();});
})();
