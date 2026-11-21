"""Визуальная проверка этапа 4г. Требуется отдельно установленный Playwright/Chromium.
Серверы: текущая ветка :8765, исходный main :8766, оба с отдельными тестовыми БД.
"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/analysis/img/stage4d'
OUT.mkdir(parents=True, exist_ok=True)

MEASURE = '''() => {
 const h=window.__qaHand, m=h.model;
 h.scene.updateMatrixWorld(true);
 const anchor=m.kind==='gltf'
   ? m.mesh.skeleton.bones.find(b=>b.name==='wrist').getWorldPosition(h.camera.position.clone())
       .lerp(m.chains.middle.bones[1].getWorldPosition(h.camera.position.clone()), .5)
   : h.handGroup.localToWorld(m.anchor.clone());
 const world=anchor.toArray(), p=anchor.project(h.camera), w=h.container.clientWidth, height=h.container.clientHeight;
 const actual=[(p.x+1)*w/2,(1-p.y)*height/2], expected=[w/2,(h.insets.top+height-h.insets.bottom)/2];
 return {kind:m.kind, hand:h.hand, world, actual, expected,
   error:Math.hypot(actual[0]-expected[0],actual[1]-expected[1]),
   damping:h.controls.enableDamping, autoRotate:h.controls.autoRotate,
   meshes:h.scene.children.filter(o=>o.isMesh).length,
   selfShadow:m.kind==='gltf' && m.mesh.castShadow && m.mesh.receiveShadow,
   anchorModel:h.handGroup.position.clone().negate().toArray()};
}'''

with sync_playwright() as p:
 browser=p.chromium.launch()
 results=[]; errors=[]
 for before,port in [(True,8766),(False,8765)]:
  for width,height in [(1366,768),(1920,1080)]:
   page=browser.new_page(viewport={'width':width,'height':height})
   print('viewport',before,width,flush=True)
   drop=[False]
   def websocket(route):
    server=route.connect_to_server()
    server.on_message(lambda message: None if drop[0] else route.send(message))
   page.route_web_socket('**/ws',websocket)
   page.add_init_script('window.WebSocket = class extends WebSocket { constructor(...args) { super(...args); window.__qaWS = this; } };')
   page.on('pageerror',lambda e: errors.append(str(e)))
   def instrument(route):
    response=route.fetch()
    text=response.text().replace('return new Hand3D(container);','return (window.__qaHand = new Hand3D(container));').replace('this.model = model;', 'this.model = model; this.__qaModelHand = this.hand;')
    route.fulfill(response=response,body=text)
   page.route('**/hand3d.js?*',instrument)
   page.request.post(f'http://127.0.0.1:{port}/api/connect',data={'kind':'mock'})
   page.request.post(f'http://127.0.0.1:{port}/api/settings',data={'hand':'right'})
   page.goto(f'http://127.0.0.1:{port}')
   page.wait_for_function('window.__qaHand?.model?.kind === "gltf"')
   page.wait_for_timeout(3000)
   page.screenshot(path=str(OUT/f'{"before" if before else "after"}-{width}.png'))
   if before:
    page.close(); continue
   assert page.locator('#sideCal').inner_text().startswith('✓ Демо:')
   assert page.locator('#connDot').count()==0
   for fallback in [False,True]:
    if fallback:
     page.route('**/*.glb?*',lambda route:route.abort())
    for hand in ['right','left']:
     page.request.post(f'http://127.0.0.1:{port}/api/settings',data={'hand':hand})
     page.wait_for_function('(args)=>window.__qaHand.__qaModelHand===args[0] && window.__qaHand.model.kind===args[1]',arg=[hand,'primitive' if fallback else 'gltf'])
     print('model',width,fallback,hand,flush=True)
     for pose,body in [('initial',{'bits':'00000','roll':0,'pitch':90,'yaw':0}),
                       ('fist',{'bits':'11111'}),('roll',{'roll':70,'pitch':0,'yaw':0}),
                       ('pitch',{'roll':0,'pitch':-70,'yaw':0}),('yaw',{'roll':0,'pitch':0,'yaw':70})]:
      page.request.post(f'http://127.0.0.1:{port}/api/mock',data=body)
      page.wait_for_timeout(1300)
      row=page.evaluate(MEASURE); row.update(width=width,height=height,pose=pose)
      results.append(row)
      assert row['error']<.01,row
      assert not row['damping'] and not row['autoRotate']
      assert row['meshes']==0 # нет отдельного пола или тени в сцене
      if row['kind']=='gltf': assert row['selfShadow']
      page.screenshot(path=str(OUT/f'after-{width}-{row["kind"]}-{hand}-{pose}.png'))
     canvas=page.locator('#hand3d canvas').bounding_box()
     x,y=canvas['x']+canvas['width']/2,canvas['y']+canvas['height']/2
     page.mouse.move(x,y); page.mouse.down(); page.mouse.move(x+150,y+70,steps=8); page.mouse.up()
     page.wait_for_timeout(100)
     first=page.evaluate('window.__qaHand.camera.position.toArray()')
     page.wait_for_timeout(350)
     assert page.evaluate('window.__qaHand.camera.position.toArray()')==first
     row=page.evaluate(MEASURE); row.update(width=width,pose='mouse'); results.append(row); assert row['error']<.01
     page.set_viewport_size({'width':width-200,'height':height-100}); page.wait_for_timeout(300)
     row=page.evaluate(MEASURE); row.update(width=width-200,pose='resize'); results.append(row); assert row['error']<.01
     page.set_viewport_size({'width':width,'height':height}); page.locator('#viewReset').click()
   page.request.post(f'http://127.0.0.1:{port}/api/disconnect')
   page.wait_for_timeout(3200)
   assert 'Нет связи с приложением' not in page.locator('#sideSrc').inner_text()
   assert 'ok' not in (page.locator('#sideDot').get_attribute('class') or '')
   page.screenshot(path=str(OUT/f'after-{width}-source-disconnected.png'))
   page.request.post(f'http://127.0.0.1:{port}/api/connect',data={'kind':'mock'})
   page.wait_for_timeout(400)
   drop[0]=True
   page.wait_for_timeout(3200)
   assert 'ok' not in (page.locator('#sideDot').get_attribute('class') or '')
   assert 'Нет связи с приложением' in page.locator('#sideSrc').inner_text()
   page.screenshot(path=str(OUT/f'after-{width}-websocket-stalled.png'))
   page.evaluate('window.__qaWS?.close()')
   page.wait_for_timeout(500)
   assert 'ok' not in (page.locator('#sideDot').get_attribute('class') or '')
   page.screenshot(path=str(OUT/f'after-{width}-websocket-disconnected.png'))
   page.close()
 (OUT/'measurements.json').write_text(json.dumps({'results':results,'pageErrors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
 assert not errors,errors
 browser.close()
 print(f'{len(results)} measurements, max error {max(r["error"] for r in results):.6g}px')
