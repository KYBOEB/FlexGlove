// Реальные glTF, матрицы three.js и функции Hand3D, без WebGL и браузера.
import vm from 'node:vm';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const web=path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../flexglove/web');
const context=vm.createContext({console,performance,URL,TextDecoder,TextEncoder,ArrayBuffer,Uint8Array,
  setTimeout,clearTimeout,window:{dispatchEvent(){}},Event:class {}});
const cache=new Map();
async function mod(filename) {
 filename=path.normalize(filename);
 if(cache.has(filename)) return cache.get(filename);
 let source=await fs.readFile(filename,'utf8');
 if(filename.endsWith('hand3d.js')) source+='\nexport {Hand3D};';
 const m=new vm.SourceTextModule(source,{context,identifier:filename,
  initializeImportMeta(meta){meta.url=new URL('file:///'+filename.replaceAll('\\','/')).href;}});
 cache.set(filename,m);
 await m.link((specifier,parent)=>mod(specifier==='three'?path.join(web,'vendor/three/three.module.min.js'):
  specifier==='flexglove/handpose'?path.join(web,'handpose.js'):
  specifier.startsWith('three/')?path.join(web,'vendor',specifier):path.resolve(path.dirname(parent.identifier),specifier)));
 return m;
}
const entry=await mod(path.join(web,'hand3d.js')); await entry.evaluate();
const {Hand3D}=entry.namespace;
const T=cache.get(path.join(web,'vendor/three/three.module.min.js')).namespace;
const {GLTFLoader}=cache.get(path.join(web,'vendor/three/addons/loaders/GLTFLoader.js')).namespace;
let count=0;
for(const hand of ['right','left']) for(const kind of ['gltf','primitive']) {
 const h=Object.create(Hand3D.prototype);
 Object.assign(h,{hand,scene:new T.Scene(),pivot:new T.Group(),handGroup:new T.Group(),
  material:new T.MeshStandardMaterial(),sensed:new Set(['thumb','index','middle','ring','pinky']),
  _fit(){},_fitLight(){}});
 h.scene.add(h.pivot); h.pivot.add(h.handGroup);
 // Переключение модели при уже повёрнутой кисти и прежнем смещении handGroup.
 h.pivot.quaternion.setFromEuler(new T.Euler(.4,-1.2,.7)); h.handGroup.position.set(-3,2,1);
 let model;
 if(kind==='gltf') {
  const bytes=await fs.readFile(path.join(web,`vendor/models/generic-hand/${hand}.glb`));
  const gltf=await new GLTFLoader().parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  model=h._prepareGltf(gltf);
 } else model=h._buildPrimitive();
 h._useModel(model);
 const fixed=h.handGroup.position.clone();
 for(const bend of [0,.5,1]) for(const axis of ['x','y','z']) for(const angle of [0,.7,1.7,-2.5]) {
  h.pivot.quaternion.setFromAxisAngle(new T.Vector3(axis==='x'?1:0,axis==='y'?1:0,axis==='z'?1:0),angle);
  for(const finger of h.sensed) h._poseFinger(finger,bend);
  h.scene.updateMatrixWorld(true);
  const anchor=kind==='gltf'?model.mesh.skeleton.bones.find(b=>b.name==='wrist').getWorldPosition(new T.Vector3())
   .lerp(model.chains.middle.bones[1].getWorldPosition(new T.Vector3()),.5):h.handGroup.localToWorld(model.anchor.clone());
  assert.ok(anchor.length()<1e-6,`${hand}/${kind}/${bend}/${axis}: ${anchor.toArray()}`);
  assert.ok(h.handGroup.position.equals(fixed));
  if(kind==='gltf') assert.ok(model.mesh.castShadow && model.mesh.receiveShadow);
  if(kind==='gltf') model.mesh.skeleton.update();
  model.root.traverse(o=>{
   if(!o.isMesh) return;
   const v=new T.Vector3();
   for(let i=0;i<o.geometry.attributes.position.count;i++) {
    o.getVertexPosition(i,v).applyMatrix4(o.matrixWorld);
    assert.ok(v.length() <= h.radius + 1e-5, `${hand}/${kind}: вершина вне сферы ${v.length()} > ${h.radius}`);
   }
  });
  count++;
 }
 assert.equal(h.scene.children.filter(o=>o.isMesh).length,0);
}
console.log(JSON.stringify({count}));
