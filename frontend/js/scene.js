/**
 * scene.js — Three.js 3D scene for Air Twin.
 *
 * Loads room.glb, purifier.glb, pm25.glb from frontend/assets/.
 * Colours the purifier based on current regime.
 * Orbit controls via mouse drag / touch.
 */

const AirTwinScene = (() => {
  let _renderer, _scene, _camera, _animId;
  let _purifierMesh = null;
  let _sensorMesh = null;
  let _currentColor = new THREE.Color('#3b82f6');
  let _targetColor = new THREE.Color('#3b82f6');

  // Orbit state
  let _orbit = { active: false, startX: 0, startY: 0, theta: 0.4, phi: 1.1, radius: 8 };

  function init() {
    const canvas = document.getElementById('three-canvas');
    if (!canvas) return;

    // Renderer
    _renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    _renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    _renderer.outputEncoding = THREE.sRGBEncoding;
    _renderer.toneMapping = THREE.ACESFilmicToneMapping;
    _renderer.toneMappingExposure = 1.2;
    _renderer.shadowMap.enabled = true;
    _renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    _renderer.setClearColor(0x000000, 0);

    // Scene
    _scene = new THREE.Scene();
    _scene.fog = new THREE.Fog(0x0a0c0f, 15, 40);

    // Camera
    _camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
    _updateCameraPos();

    // Lights
    const ambient = new THREE.AmbientLight(0xffffff, 0.4);
    _scene.add(ambient);

    const dirLight = new THREE.DirectionalLight(0xffffff, 1.0);
    dirLight.position.set(5, 8, 5);
    dirLight.castShadow = true;
    dirLight.shadow.mapSize.set(1024, 1024);
    dirLight.shadow.camera.near = 0.5;
    dirLight.shadow.camera.far = 30;
    _scene.add(dirLight);

    const fillLight = new THREE.DirectionalLight(0x8fb4ff, 0.3);
    fillLight.position.set(-4, 3, -3);
    _scene.add(fillLight);

    // Load assets
    _loadGLB('assets/room.glb', (gltf) => {
      const room = gltf.scene;
      room.traverse(child => {
        if (child.isMesh) {
          child.receiveShadow = true;
          // Schematic style — override with flat material
          child.material = new THREE.MeshStandardMaterial({
            color: 0x1a1f2e,
            roughness: 0.9,
            metalness: 0.0,
            side: THREE.FrontSide,
          });
        }
      });
      _scene.add(room);
      _fitCameraToScene();
    });

    _loadGLB('assets/purifier.glb', (gltf) => {
      _purifierMesh = gltf.scene;
      _purifierMesh.traverse(child => {
        if (child.isMesh) {
          child.castShadow = true;
          child.material = new THREE.MeshStandardMaterial({
            color: 0x22c55e,
            roughness: 0.4,
            metalness: 0.2,
            emissive: new THREE.Color(0x22c55e),
            emissiveIntensity: 0.15,
          });
        }
      });
      _scene.add(_purifierMesh);
      _registerAssetMesh(_purifierMesh, 'starkvind_01', 'purifier');
    });

    _loadGLB('assets/pm25.glb', (gltf) => {
      _sensorMesh = gltf.scene;
      _sensorMesh.traverse(child => {
        if (child.isMesh) {
          child.castShadow = true;
          child.material = new THREE.MeshStandardMaterial({
            color: 0x3b82f6,
            roughness: 0.5,
            metalness: 0.3,
            emissive: new THREE.Color(0x3b82f6),
            emissiveIntensity: 0.2,
          });
        }
      });
      _scene.add(_sensorMesh);
      _registerAssetMesh(_sensorMesh, 'sds011_01', 'sensor');
    });

    // Orbit controls
    _initOrbit(canvas);

    // Resize observer
    const resizeObs = new ResizeObserver(_onResize);
    resizeObs.observe(canvas.parentElement);
    _onResize();

    // Subscribe to state changes
    AirTwinState.on('update', _onStateUpdate);
    AirTwinState.on('regime-change', _onRegimeChange);

    // Start render loop
    _animate();

    // Hide hint after interaction
    canvas.addEventListener('mousedown', _hideHint, { once: true });
    canvas.addEventListener('touchstart', _hideHint, { once: true });
  }

  // ── Asset hover tooltip ─────────────────────────────────────

  let _raycaster = null;
  let _mouse = new THREE.Vector2();
  let _hoveredAsset = null;
  let _tooltipAssets = []; // meshes with asset metadata

  function _initTooltip(canvas) {
    _raycaster = new THREE.Raycaster();

    canvas.addEventListener('mousemove', (e) => {
      const rect = canvas.getBoundingClientRect();
      _mouse.x = ((e.clientX - rect.left) / rect.width)  * 2 - 1;
      _mouse.y = -((e.clientY - rect.top)  / rect.height) * 2 + 1;
      _updateTooltip(e.clientX, e.clientY);
    });

    canvas.addEventListener('mouseleave', () => {
      _hideTooltip();
    });
  }

  function _registerAssetMesh(mesh, assetId, assetType) {
    mesh.traverse(child => {
      if (child.isMesh) {
        child.userData.assetId = assetId;
        child.userData.assetType = assetType;
        _tooltipAssets.push(child);
      }
    });
  }

  function _updateTooltip(mouseX, mouseY) {
    if (!_raycaster || !_camera || _tooltipAssets.length === 0) return;

    _raycaster.setFromCamera(_mouse, _camera);
    const intersects = _raycaster.intersectObjects(_tooltipAssets, false);

    const tooltip = document.getElementById('asset-tooltip');
    if (!tooltip) return;

    if (intersects.length > 0) {
      const hit = intersects[0].object;
      const assetId = hit.userData.assetId;
      const assetType = hit.userData.assetType;

      if (assetId !== _hoveredAsset) {
        _hoveredAsset = assetId;
        const state = AirTwinState.get();

        // Build tooltip content
        const titleEl = document.getElementById('tooltip-title');
        const statusEl = document.getElementById('tooltip-status');
        const pm25El = document.getElementById('tooltip-pm25');
        const fanEl = document.getElementById('tooltip-fan');

        if (titleEl) {
          const label = assetType === 'sensor' ? 'SDS011 · ' : 'STARKVIND · ';
          titleEl.textContent = label + assetId;
        }

        if (statusEl) {
          const status = state.asset_status || 'unknown';
          statusEl.textContent = 'Status: ' + status.replace(/_/g, ' ');
        }

        if (pm25El) {
          pm25El.textContent = state.pm25 != null ?
            `PM2.5: ${state.pm25.toFixed(1)} µg/m³` : 'PM2.5: —';
        }

        if (fanEl) {
          if (assetType === 'purifier') {
            const mode = state.fan_mode || '—';
            const speed = state.fan_speed || '—';
            fanEl.textContent = `Fan: ${mode} · Step ${speed}`;
          } else {
            fanEl.textContent = `Sensor · ${state.regime?.toUpperCase() || '—'}`;
          }
        }
      }

      // Position tooltip near cursor
      tooltip.classList.remove('hidden');
      tooltip.style.left = `${mouseX + 16}px`;
      tooltip.style.top  = `${mouseY - 8}px`;

      // Keep tooltip on screen
      const rect = tooltip.getBoundingClientRect();
      if (rect.right > window.innerWidth - 8) {
        tooltip.style.left = `${mouseX - rect.width - 16}px`;
      }
      if (rect.bottom > window.innerHeight - 8) {
        tooltip.style.top = `${mouseY - rect.height - 8}px`;
      }

    } else {
      _hideTooltip();
    }
  }

  function _hideTooltip() {
    _hoveredAsset = null;
    const tooltip = document.getElementById('asset-tooltip');
    if (tooltip) tooltip.classList.add('hidden');
  }


  // ── GLB loader ──────────────────────────────────────────────

  function _loadGLB(url, onLoad) {
    // Manual GLB loader using fetch + THREE.ObjectLoader fallback
    // Uses the built-in three.js GLTFLoader pattern
    if (!THREE.GLTFLoader) {
      // Fallback: load via script tag dynamically
      _loadGLTFLoaderScript(() => _loadGLB(url, onLoad));
      return;
    }
    const loader = new THREE.GLTFLoader();
    loader.load(url, onLoad, undefined, (err) => {
      console.warn(`Failed to load ${url}:`, err);
    });
  }

  function _loadGLTFLoaderScript(cb) {
    const script = document.createElement('script');
    script.src = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/examples/js/loaders/GLTFLoader.js';
    script.onload = cb;
    script.onerror = () => {
      // Try local fallback
      console.warn('GLTFLoader CDN failed — trying inline loader');
      _inlineGLTFLoad(cb);
    };
    document.head.appendChild(script);
  }

  function _inlineGLTFLoad(cb) {
    // Minimal GLB loader for r128 — handles binary glTF
    THREE.GLTFLoader = class {
      load(url, onLoad, onProgress, onError) {
        fetch(url)
          .then(r => r.arrayBuffer())
          .then(buffer => {
            const view = new DataView(buffer);
            // GLB magic check
            if (view.getUint32(0, true) !== 0x46546C67) {
              throw new Error('Not a valid GLB file');
            }
            // Parse JSON chunk
            const jsonLen = view.getUint32(12, true);
            const jsonBytes = new Uint8Array(buffer, 20, jsonLen);
            const json = JSON.parse(new TextDecoder().decode(jsonBytes));

            // Parse binary chunk if present
            let binBuffer = null;
            const binOffset = 20 + jsonLen;
            if (binOffset < buffer.byteLength) {
              const binLen = view.getUint32(binOffset, true);
              binBuffer = buffer.slice(binOffset + 8, binOffset + 8 + binLen);
            }

            const scene = _parseGLTF(json, binBuffer);
            onLoad({ scene });
          })
          .catch(onError);
      }
    };
    cb();
  }

  function _parseGLTF(json, binBuffer) {
    const group = new THREE.Group();

    if (!json.meshes || !json.meshes.length) return group;

    json.meshes.forEach(mesh => {
      mesh.primitives.forEach(prim => {
        const geom = _parseGeometry(json, prim, binBuffer);
        if (!geom) return;
        const mat = new THREE.MeshStandardMaterial({
          color: 0x888888, roughness: 0.7, metalness: 0.1
        });
        const obj = new THREE.Mesh(geom, mat);
        group.add(obj);
      });
    });

    return group;
  }

  function _parseGeometry(json, prim, binBuffer) {
    if (!prim.attributes) return null;
    const geom = new THREE.BufferGeometry();

    const getAccessor = (idx) => {
      const acc = json.accessors[idx];
      const bv = json.bufferViews[acc.bufferView];
      const offset = (bv.byteOffset || 0) + (acc.byteOffset || 0);
      const typeMap = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 };
      const count = acc.count * (typeMap[acc.type] || 1);
      const ArrayType = acc.componentType === 5123 ? Uint16Array :
                        acc.componentType === 5125 ? Uint32Array : Float32Array;
      return new ArrayType(binBuffer, offset, count);
    };

    if (prim.attributes.POSITION !== undefined) {
      geom.setAttribute('position', new THREE.BufferAttribute(
        getAccessor(prim.attributes.POSITION), 3
      ));
    }
    if (prim.attributes.NORMAL !== undefined) {
      geom.setAttribute('normal', new THREE.BufferAttribute(
        getAccessor(prim.attributes.NORMAL), 3
      ));
    }
    if (prim.indices !== undefined) {
      geom.setIndex(new THREE.BufferAttribute(
        getAccessor(prim.indices), 1
      ));
    }
    geom.computeBoundingBox();
    return geom;
  }

  // ── Camera ──────────────────────────────────────────────────

  function _updateCameraPos() {
    const { theta, phi, radius } = _orbit;
    _camera.position.set(
      radius * Math.sin(phi) * Math.sin(theta),
      radius * Math.cos(phi),
      radius * Math.sin(phi) * Math.cos(theta)
    );
    _camera.lookAt(0, 1, 0);
  }

  function _fitCameraToScene() {
    const box = new THREE.Box3().setFromObject(_scene);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    _orbit.radius = Math.max(size.x, size.y, size.z) * 1.8;
    _camera.lookAt(center);
    _updateCameraPos();
  }

  // ── Orbit controls ──────────────────────────────────────────

  function _initOrbit(canvas) {
    canvas.addEventListener('mousedown', e => {
      _orbit.active = true;
      _orbit.startX = e.clientX;
      _orbit.startY = e.clientY;
    });
    window.addEventListener('mousemove', e => {
      if (!_orbit.active) return;
      const dx = (e.clientX - _orbit.startX) * 0.005;
      const dy = (e.clientY - _orbit.startY) * 0.005;
      _orbit.theta -= dx;
      _orbit.phi = Math.max(0.2, Math.min(Math.PI / 2, _orbit.phi + dy));
      _orbit.startX = e.clientX;
      _orbit.startY = e.clientY;
      _updateCameraPos();
    });
    window.addEventListener('mouseup', () => { _orbit.active = false; });

    canvas.addEventListener('wheel', e => {
      _orbit.radius = Math.max(3, Math.min(20, _orbit.radius + e.deltaY * 0.01));
      _updateCameraPos();
    }, { passive: true });

    // Touch support
    let lastTouchDist = 0;
    canvas.addEventListener('touchstart', e => {
      if (e.touches.length === 1) {
        _orbit.active = true;
        _orbit.startX = e.touches[0].clientX;
        _orbit.startY = e.touches[0].clientY;
      } else if (e.touches.length === 2) {
        lastTouchDist = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        );
      }
    }, { passive: true });

    canvas.addEventListener('touchmove', e => {
      if (e.touches.length === 1 && _orbit.active) {
        const dx = (e.touches[0].clientX - _orbit.startX) * 0.006;
        const dy = (e.touches[0].clientY - _orbit.startY) * 0.006;
        _orbit.theta -= dx;
        _orbit.phi = Math.max(0.2, Math.min(Math.PI / 2, _orbit.phi + dy));
        _orbit.startX = e.touches[0].clientX;
        _orbit.startY = e.touches[0].clientY;
        _updateCameraPos();
      } else if (e.touches.length === 2) {
        const dist = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        );
        _orbit.radius = Math.max(3, Math.min(20, _orbit.radius - (dist - lastTouchDist) * 0.02));
        lastTouchDist = dist;
        _updateCameraPos();
      }
    }, { passive: true });

    canvas.addEventListener('touchend', () => { _orbit.active = false; });
  }

  // ── State updates ───────────────────────────────────────────

  function _onStateUpdate(state) {
    const color = AirTwinState.regimeColor(state.regime, state.confidence);
    _targetColor = new THREE.Color(color);
  }

  function _onRegimeChange({ from, to }) {
    // Flash the purifier mesh on regime change
    if (_purifierMesh) {
      _purifierMesh.traverse(child => {
        if (child.isMesh) {
          const orig = child.material.emissiveIntensity;
          child.material.emissiveIntensity = 0.8;
          setTimeout(() => {
            if (child.material) child.material.emissiveIntensity = 0.15;
          }, 300);
        }
      });
    }
  }

  // ── Resize ──────────────────────────────────────────────────

  function _onResize() {
    const container = document.getElementById('viewport-container');
    if (!container || !_renderer || !_camera) return;
    const w = container.clientWidth;
    const h = container.clientHeight;
    _renderer.setSize(w, h);
    _camera.aspect = w / h;
    _camera.updateProjectionMatrix();
  }

  // ── Render loop ─────────────────────────────────────────────

  function _animate() {
    _animId = requestAnimationFrame(_animate);

    // Smooth colour interpolation
    _currentColor.lerp(_targetColor, 0.05);

    // Apply to purifier mesh
    if (_purifierMesh) {
      _purifierMesh.traverse(child => {
        if (child.isMesh && child.material) {
          child.material.color.copy(_currentColor);
          child.material.emissive.copy(_currentColor);
        }
      });
    }

    if (_renderer && _scene && _camera) {
      _renderer.render(_scene, _camera);
    }
  }

  function _hideHint() {
    const hint = document.getElementById('viewport-hint');
    if (hint) {
      hint.style.opacity = '0';
      setTimeout(() => { hint.style.display = 'none'; }, 500);
    }
  }

  return { init };
})();

// Init after DOM and state ready
document.addEventListener('DOMContentLoaded', () => {
  AirTwinScene.init();
});