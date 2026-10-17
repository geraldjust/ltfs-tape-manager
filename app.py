import os, json, time, uuid, shutil, hashlib, threading, re, subprocess, glob
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory
import docker
from docker.types import Mount

app=Flask(__name__,static_folder='static')
client=docker.from_env()
TAPE=Path('/tape'); SRC=Path('/write-source'); DEST=Path('/destination'); STATE=Path('/state-simple')
STATE.mkdir(parents=True,exist_ok=True)
lock=threading.Lock()
REFRESH_CACHE={'data':None,'time':0}
HEALTH_CACHE={'data':None,'time':0}
RESTART_PENDING=False
job={'state':'idle','message':'Ready','done':0,'total':0,'file':'','bytes_done':0,'bytes_total':0}
IMAGE=os.environ.get('AURORA_IMAGE','ghcr.io/auroratape/aurora-ltfs:1.0.0')
HOST_TAPE=os.environ.get('HOST_TAPE_PATH','/mnt/example/LTFS')
HOST_SOURCE=os.environ.get('HOST_WRITE_PATH','/mnt/example/LTFS-WRITE')
HOST_DEST=os.environ.get('HOST_READ_PATH','/mnt/example/LTFS-READ')
SG_DEVICE=os.environ.get('SG_DEVICE','/dev/sgX')
NST_DEVICE=os.environ.get('NST_DEVICE','/dev/nstX')
WORKER_NAME=os.environ.get('LTFS_WORKER_NAME','aurora-ltfs')
MANAGER_NAME=os.environ.get('LTFS_MANAGER_NAME','ltfs-simple-manager')
VERSION='1.2.0.5'

def save():
    tmp=STATE/'operation.tmp';tmp.write_text(json.dumps(job));os.replace(tmp,STATE/'operation.json')
def update(**kwargs):
    job.update(kwargs);job['updated_at']=datetime.now(timezone.utc).isoformat();save()
# statvfs on the destination mount reports space available to this filesystem,
# including ZFS dataset availability constraints exposed by the mounted filesystem.
# Do not use pool-wide free space to authorize a restore.
def destination_space():
    if not DEST.is_dir() or DEST.is_symlink():
        raise RuntimeError('Restore destination is missing or unsafe')
    st=os.statvfs(DEST)
    free=st.f_bavail*st.f_frsize
    reserve=max(10*1024**3, int(free*.05))
    return {'free':free,'reserve':reserve,'usable':max(0,free-reserve),
            'total':st.f_blocks*st.f_frsize}

def require_destination_space(remaining, context='restore'):
    info=destination_space()
    if remaining>info['usable']:
        raise RuntimeError('Insufficient restore destination space during '+context+
          ': need '+str(remaining)+' bytes, available '+str(info['free'])+
          ' bytes, safety reserve '+str(info['reserve'])+' bytes')
    return info

def mounts():
    rows=[]
    with open('/proc/self/mounts') as f:
        for line in f:
            x=line.split()
            if len(x)>3 and x[1]=='/tape' and x[0].startswith('ltfs:') and x[2].startswith('fuse'):
                rows.append(x)
    return rows

def aurora():
    try:
        c=client.containers.get(WORKER_NAME);c.reload();return c
    except docker.errors.NotFound:return None

def mode():
    c=aurora()
    if not c or c.status!='running':return 'unmounted'
    try:
        result=c.exec_run(['cat','/proc/self/mounts'],demux=False)
        if result.exit_code!=0:return 'mounting'
        for line in result.output.decode('utf-8','replace').splitlines():
            parts=line.split()
            if len(parts)>3 and parts[1]=='/ltfs' and parts[0].startswith('ltfs:') and parts[2].startswith('fuse'):
                return 'ro' if 'ro' in parts[3].split(',') else 'rw'
    except Exception:return 'mounting'
    return 'mounting'

def wait_mode(target, seconds=90):
    # Tape load, positioning and LTFS index reads can exceed two minutes.
    # Use a monotonic deadline (Docker calls themselves can take time) and
    # require repeated observations of the actual FUSE mount before success.
    deadline=time.monotonic()+seconds
    stable=0
    last='unknown'
    while time.monotonic()<deadline:
        c=aurora()
        if target=='unmounted':
            if not c or c.status!='running':
                return
        else:
            if not c:
                raise RuntimeError('LTFS worker disappeared while waiting for '+target)
            if c.status in ('exited','dead'):
                detail=c.logs(tail=60).decode('utf-8','replace')[-4000:]
                raise RuntimeError('LTFS worker exited before '+target+' mount: '+detail)
        last=mode()
        if last==target:
            stable+=1
            if stable>=3:
                return
        else:
            stable=0
        time.sleep(1)
    # One final check avoids misreporting a mount that completed at the deadline.
    if mode()==target:
        return
    detail=''
    c=aurora()
    if c:
        try: detail=c.logs(tail=35).decode('utf-8','replace')[-2500:]
        except Exception: pass
    raise RuntimeError(f'LTFS did not reach {target} mode within {seconds}s (now {mode()}). Recent Aurora logs: '+detail)

def stop_aurora():
    c=aurora()
    if c:
        if c.status=='running':c.stop(timeout=300)
        c.remove()
    wait_mode('unmounted',30)

def mount_aurora(rw=False,allow_stale=False):
    desired='rw' if rw else 'ro'
    if mode()==desired:return
    if mounts() and mode()=='unmounted' and not allow_stale:
        raise RuntimeError('Stale LTFS bind mount detected; restart the Simple Manager container before mounting.')
    stop_aurora()
    cmd=['altfs','-f','-o','devname='+SG_DEVICE,'-o','noatime']
    cmd+=['-o','sync_type=unmount'] if rw else ['-o','ro']
    cmd+=['/ltfs']
    client.containers.run(IMAGE,command=cmd,name=WORKER_NAME,detach=True,privileged=True,
        devices=[SG_DEVICE+':'+SG_DEVICE+':rwm','/dev/fuse:/dev/fuse:rwm'],
        mounts=[Mount(target='/ltfs',source=HOST_TAPE,type='bind',propagation='rshared'),
                Mount(target='/source',source=HOST_SOURCE,type='bind',read_only=True)],
        security_opt=['apparmor:unconfined'],restart_policy={'Name':'no'})
    wait_mode(desired,600)

def safe_path(base,relative):
    p=(base/relative).resolve(); b=base.resolve()
    if p!=b and b not in p.parents:raise ValueError('Invalid path')
    return p

def files_under(base):
    items=[]
    for root,dirs,files in os.walk(base):
        dirs[:]=sorted(d for d in dirs if not (Path(root)/d).is_symlink())
        for name in sorted(files):
            p=Path(root)/name
            if p.is_symlink() or not p.is_file():continue
            items.append((p,p.relative_to(base),p.stat().st_size))
    return items

def hash_file(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        while True:
            b=f.read(4*1024*1024)
            if not b:break
            h.update(b)
    return h.hexdigest()

def copy_file(src,dst):
    h=hashlib.sha256();dst.parent.mkdir(parents=True,exist_ok=True)
    with open(src,'rb') as a,open(dst,'xb') as b:
        while True:
            chunk=a.read(4*1024*1024)
            if not chunk:break
            b.write(chunk);h.update(chunk)
            update(bytes_done=job['bytes_done']+len(chunk))
    return h.hexdigest()

def worker(kind,opts):
    try:
        if kind=='read':
            if mode()!='ro':mount_aurora(False)
            source=safe_path(TAPE,opts['path'])
            if not source.exists():raise RuntimeError('Source does not exist')
            chosen=files_under(source) if source.is_dir() else [(source,Path(source.name),source.stat().st_size)]
            if not chosen:raise RuntimeError('No files to restore')
            out=DEST/('LTFS_RESTORE_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
            update(total=len(chosen),bytes_total=sum(x[2] for x in chosen))
            for i,(p,rel,n) in enumerate(chosen):
                update(file=str(rel),message='Restoring files')
                dst=out/rel;digest=copy_file(p,dst)
                if hash_file(dst)!=digest:raise RuntimeError('Restore verification failed: '+str(rel))
                update(done=i+1)
            update(state='completed',message='Restore verified: '+str(out))
        elif kind=='format':
            label=opts['label']
            tape_serial=opts.get('tape_serial','')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',label):raise RuntimeError('Invalid volume label')
            if tape_serial and not re.fullmatch(r'[A-Za-z0-9]{6}',tape_serial):raise RuntimeError('Tape serial must be exactly six alphanumeric characters')
            if mode()!='unmounted':raise RuntimeError('Unmount LTFS before formatting')
            if busy_mount_users():raise RuntimeError('Tape busy; formatting refused')
            if drive_presence()!='inserted':raise RuntimeError('Cartridge presence not confirmed; formatting refused')
            update(message='Formatting tape; do not interrupt or remove cartridge')
            c=aurora()
            if c:stop_aurora()
            # Format is deliberately never retried automatically.
            format_cmd=['mkaltfs','--device='+SG_DEVICE,'--volume-name='+label,'--force']
            if tape_serial:format_cmd.append('--tape-serial='+tape_serial)
            container=client.containers.create(IMAGE,command=format_cmd,
                privileged=True,devices=[SG_DEVICE+':'+SG_DEVICE+':rwm'],
                network_disabled=True)
            try:
                container.start()
                result=container.wait(timeout=7200)
                logs=container.logs(stdout=True,stderr=True).decode('utf-8','replace')
                if result.get('StatusCode')!=0:
                    (STATE/'last-format-log.txt').write_text(logs[-30000:])
                    raise RuntimeError('Formatting failed (exit '+str(result.get('StatusCode'))+'). See last-format-log.txt in state directory; do not retry automatically.')
                update(message='Format command completed; mounting read-only to verify')
            finally:
                try:container.remove(force=False)
                except Exception:pass
            mount_aurora(False)
            if mode()!='ro':raise RuntimeError('Format completed but read-only verification mount failed')
            update(state='completed',message='Formatted and mounted read-only: '+label)
    except Exception as e:
        update(state='failed',message=str(e))
        # Never silently attempt a second format or write.
    finally:
        if job.get('state')=='running':update(state='failed',message='Operation stopped unexpectedly')
        lock.release()

@app.get('/')
def home():return send_from_directory('static','index.html')
def command(argv, timeout=12):
    try:
        p=subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {'ok':p.returncode==0,'output':(p.stdout+'\n'+p.stderr).strip()[-6000:]}
    except Exception as e:return {'ok':False,'output':str(e)}

DRIVE_CACHE=STATE/'drive-info.json'

def cached_drive():
    try:
        data=json.loads(DRIVE_CACHE.read_text())
        if isinstance(data,dict) and isinstance(data.get('fields'),dict):return data
    except (OSError,ValueError,TypeError):pass
    return {'fields':{},'ok':False,'source':'unavailable','captured_at':None}

def inquiry():
    r=command(['sg_inq',SG_DEVICE])
    out=r['output'];fields={}
    for key in ('Vendor identification','Product identification','Product revision level','Unit serial number'):
        match=re.search(r'^\s*'+re.escape(key)+r':\s*(.*)$',out,re.M|re.I)
        if match:fields[key]=match.group(1).strip()
    serial=command(['sg_inq','-p','0x80',SG_DEVICE])
    match=re.search(r'Unit serial number:\s*(\S+)',serial['output'],re.I)
    if match:fields['Unit serial number']=match.group(1)
    else:
        # sg_inq -p 0x80 may print a hex dump rather than a decoded serial.
        hexbytes=re.findall(r'(?m)^\s*(?:[0-9a-f]{2}\s+){5,}',serial['output'],re.I)
        if hexbytes:
            try:
                raw=bytes.fromhex(' '.join(hexbytes).split('  ....')[0])
                if len(raw)>=4:
                    length=int.from_bytes(raw[2:4],'big')
                    val=raw[4:4+length].decode('ascii','ignore').strip()
                    if val and val.isprintable():fields['Unit serial number']=val
            except (ValueError,IndexError):pass
    if fields and r['ok']:
        data={'fields':fields,'raw':out,'serial_raw':serial['output'],'ok':True,
              'source':'live','captured_at':datetime.now(timezone.utc).isoformat()}
        try:
            tmp=STATE/'drive-info.tmp'
            tmp.write_text(json.dumps(data));os.replace(tmp,DRIVE_CACHE)
        except OSError:pass
        return data
    data=cached_drive()
    data['source']='last_known' if data.get('fields') else 'unavailable'
    return data

TEMPERATURE_CACHE={'data':None,'time':0}

def drive_temperature():
    """Best-effort temperature query while drive is idle and LTFS unmounted.

    Never report unsupported when the command failed or its output was unrecognized.
    """
    now=time.monotonic()
    cached=TEMPERATURE_CACHE['data']
    if lock.locked() or job.get('state')=='running' or mode()!='unmounted':
        return cached or {'celsius':None,'source':'Not checked (LTFS mounted or operation active)','checked_at':None}
    if cached and now-TEMPERATURE_CACHE['time']<30:return cached
    result=command(['sg_logs','-p','0x0d',SG_DEVICE],timeout=8)
    value=None
    if result['ok']:
        for line in result['output'].splitlines():
            # sg3_utils output varies by version: Current temperature: 38 C,
            # Current temperature = 38, or Temperature: 38 Celsius.
            match=re.search(r'(?i)\b(?:current\s+temperature|temperature)\s*[:=]\s*(-?\d+)\s*(?:degrees?\s*)?(?:celsius|c)?\b',line)
            if match:
                candidate=int(match.group(1))
                if -20<=candidate<=100:value=candidate
                break
    if value is not None:source='SCSI temperature log'
    elif not result['ok']:source='Temperature query failed or page unavailable'
    else:source='Temperature not found in drive response'
    data={'celsius':value,'source':source,'checked_at':datetime.now(timezone.utc).isoformat()}
    TEMPERATURE_CACHE.update(data=data,time=now)
    return data

def drive_health():
    now=time.monotonic()
    if lock.locked():return HEALTH_CACHE['data'] or {'cleaning':'Unknown','alerts':[],'source':'unavailable'}
    if HEALTH_CACHE['data'] and now-HEALTH_CACHE['time']<30:return HEALTH_CACHE['data']
    # Avoid LOG SENSE while Aurora has the drive open.
    if mode()!='unmounted':return HEALTH_CACHE['data'] or {'cleaning':'Unknown','alerts':[],'source':'unavailable'}
    r=command(['sg_logs','-p','0x2e',SG_DEVICE],timeout=8)
    out=r['output']
    flags={}
    names={}
    for line in out.splitlines():
        # sg_logs decoded TapeAlert output: "Cleaning required: 0"
        m=re.match(r'^\s*([A-Za-z][A-Za-z0-9 /()_-]+):\s*([01])\s*$',line)
        if m:names[m.group(1).strip().lower()]=int(m.group(2))
        m=re.search(r'(?:parameter code|parameter)\s*[:=]?\s*(?:0x)?([0-9a-f]{1,4})',line,re.I)
        if m:
            try:flags[int(m.group(1),16)]=bool(re.search(r'(?:value|hex)\s*[:=]?\s*(?:0x)?0*1\b',line,re.I))
            except ValueError:pass
    requested=[names.get('cleaning required'),names.get('cleaning requested')]
    if r['ok'] and all(x is not None for x in requested):
        cleaning='Yes' if any(requested) else 'No'
    elif r['ok'] and 20 in flags and 21 in flags:
        cleaning='Yes' if flags[20] or flags[21] else 'No'
    else:cleaning='Unknown'
    active_names=[k for k,v in names.items() if v]
    data={'cleaning':cleaning,'alerts':active_names or [str(k) for k,v in flags.items() if v],
          'source':'SCSI TapeAlert' if r['ok'] else 'unavailable',
          'checked_at':datetime.now(timezone.utc).isoformat(),'raw':out[-6000:]}
    HEALTH_CACHE.update(data=data,time=now)
    return data

def busy_mount_users():
    # Fail closed if a second running container has this LTFS dataset mounted.
    try:
        for c in client.containers.list():
            if c.name in (MANAGER_NAME,WORKER_NAME):continue
            for mount in c.attrs.get('Mounts',[]):
                if mount.get('Source')==HOST_TAPE:
                    return 'Another running container is using the LTFS mount: '+c.name
    except Exception as e:return 'Unable to check other container users: '+str(e)
    # fuser checks processes visible in the GUI's PID namespace; we also check
    # the host PID namespace via pid: host in Compose.
    if mode() in ('ro','rw'):
        r=command(['fuser','-m','/tape'],timeout=8)
        pids=re.findall(r'\b\d+\b',r['output'])
        own=str(os.getpid())
        pids=[p for p in pids if p!=own]
        if pids:return 'Processes have the tape mount open: '+', '.join(pids[:12])
        if not r['ok'] and 'Permission denied' in r['output']:
            return 'Cannot verify whether the tape is busy'
    return None

def restart_gui_later():
    # Recreating the mount namespace is required after FUSE unmount because
    # the GUI may retain a stale /tape entry despite host unmount success.
    def restart():
        time.sleep(2)
        try:client.containers.get('ltfs-simple-manager').restart(timeout=10)
        except Exception as e:print('Automatic GUI restart failed:',e,flush=True)
    threading.Thread(target=restart,daemon=True).start()

LAST_MOUNT_FAILURE=STATE/'last-mount-failure.json'

def record_mount_failure(message):
    try:LAST_MOUNT_FAILURE.write_text(json.dumps({'message':message,'at':datetime.now(timezone.utc).isoformat()}))
    except OSError:pass

def mount_failure():
    try:return json.loads(LAST_MOUNT_FAILURE.read_text())
    except (OSError,ValueError):return {}

def drive_presence():
    # Readiness via TEST UNIT READY; mt status bitfields vary across drivers.
    # Drive responding does not mean a cartridge is present.
    result=command(['mt','-f',NST_DEVICE,'status'],timeout=12)
    output=(result.get('output','') or '').lower()
    if any(x in output for x in ('medium not present','medium not loaded','no medium','no tape','not ready')):return 'empty'
    if mode() in ('ro','rw'):return 'inserted'
    # A failed readiness query must not be misreported as inserted.
    turs=command(['sg_turs',SG_DEVICE],timeout=10)
    raw=(turs.get('output','') or '').lower()
    if any(x in raw for x in ('medium not present','no medium','medium not loaded','not ready','asc=0x3a','asc=3a','logical unit not ready')):return 'empty'
    if turs.get('ok'):return 'inserted'
    return 'unknown'

def system_state(m,active,presence):
    action=job.get('action','')
    if active:
        if action=='eject':return 'Drive ejecting tape'
        if action=='unmount':return 'Drive unloading LTFS'
        if action=='format':return 'Tape inserted — Formatting LTFS'
        if action=='write':return 'Tape inserted — Verifying LTFS data' if job.get('stage')=='verifying' else 'Tape inserted — LTFS writing'
        if action=='read':return 'Tape inserted — LTFS reading'
        if action=='mount_ro':return 'Tape inserted — Mounting LTFS'
    if m in ('ro','rw'):return 'Tape inserted — LTFS mounted'
    if presence=='empty':return 'Drive ready — No tape'
    if presence=='inserted':
        failure=mount_failure().get('message','').lower()
        if 'end-of-data' in failure or 'eod' in failure:return 'Tape inserted — LTFS format unconfirmed (mount failed)'
        return 'Tape inserted — LTFS format not yet checked (unmounted)'
    return 'Drive not ready'

def mounted_volume_identity(m):
    """Only report identity when confirmed from the currently running LTFS mount.

    Do not infer a tape label from the last format request: cartridges can change.
    """
    if m not in ('ro','rw'):
        return {'volume_name':None,'barcode':None,'source':'unavailable'}
    c=aurora()
    if not c or c.status!='running':
        return {'volume_name':None,'barcode':None,'source':'unavailable'}
    try:
        logs=c.logs(tail=300).decode('utf-8','replace')
    except Exception:
        return {'volume_name':None,'barcode':None,'source':'unavailable'}
    # Accept only explicit LTFS metadata fields, not names from a command or error.
    patterns={
        'volume_name':[r'(?im)^.*?\bVolume name\s*[:=]\s*["\']?([A-Za-z0-9_.-]{1,64})',
                       r'(?im)^.*?\bVolume label\s*[:=]\s*["\']?([A-Za-z0-9_.-]{1,64})'],
        'barcode':[r'(?im)^.*?\b(?:Tape serial|Barcode|Volume serial)\s*[:=]\s*([A-Za-z0-9]{6})\b'],
        'format_version':[r'(?im)Tape attribute: Application Format Version\s*=\s*([0-9.]+)'],
        'application_version':[r'(?im)Tape attribute: Application Version\s*=\s*([0-9.]+)'],
        'current_software':[r'(?im)Aurora LTFS version\s+([0-9.]+)']
    }
    result={'volume_name':None,'barcode':None,'format_version':None,'application_version':None,'current_software':None,'formatting_program':None,'source':'LTFS mount logs'}
    for key,rxs in patterns.items():
        for rx in rxs:
            matches=re.findall(rx,logs)
            if matches:
                result[key]=matches[-1]
                break
    return result

def status_data():
    m=mode();capacity={}
    if m in ('ro','rw'):
        try:
            st=os.statvfs(TAPE);capacity={'total':st.f_blocks*st.f_frsize,'free':st.f_bavail*st.f_frsize}
            capacity['used']=capacity['total']-capacity['free']
        except OSError:pass
    c=aurora()
    # Status endpoint does not issue SCSI commands while a tape operation is active.
    active=lock.locked() or job.get('state')=='running'
    drive=cached_drive() if active or m!='unmounted' else (inquiry() if time.monotonic()-REFRESH_CACHE['time']>30 else cached_drive())
    if not active and m=='unmounted' and time.monotonic()-REFRESH_CACHE['time']>30:REFRESH_CACHE['time']=time.monotonic()
    if active or m!='unmounted':drive['source']='last_known' if drive.get('fields') else 'unavailable'
    temperature=drive_temperature() if not active else (TEMPERATURE_CACHE['data'] or {'celsius':None,'source':'unavailable','checked_at':None})
    health=HEALTH_CACHE['data'] if active else drive_health()
    if health is None:health={'cleaning':'Unknown','alerts':[],'source':'unavailable'}
    tape={'ok':False,'output':'Unavailable while LTFS is mounted or operation active'}
    if not active and m=='unmounted' and time.monotonic()-REFRESH_CACHE.get('tape_time',0)>30:
        REFRESH_CACHE['tape']=command(['mt','-f',NST_DEVICE,'status']);REFRESH_CACHE['tape_time']=time.monotonic()
    if not active and m=='unmounted':tape=REFRESH_CACHE.get('tape',tape)
    if not active and m=='unmounted' and time.monotonic()-REFRESH_CACHE.get('presence_time',0)>30:
        REFRESH_CACHE['presence']=drive_presence();REFRESH_CACHE['presence_time']=time.monotonic()
    presence='inserted' if m in ('ro','rw') else REFRESH_CACHE.get('presence','unknown')
    return dict(ok=True,version=VERSION,mount=m,volume_identity=mounted_volume_identity(m),aurora=c.status if c else 'missing',capacity=capacity,system_state=system_state(m,active,presence),presence=presence,mount_failure=mount_failure(),
        operation=job,drive=drive,health=health,temperature=temperature,tape=tape,
        paths={'read':HOST_DEST,'write':HOST_SOURCE},controls_busy=active)

@app.get('/api/status')
def status():return jsonify(status_data())

@app.post('/api/control')
def control():
    data=request.get_json(silent=True) or {};action=data.get('action')
    if action not in ('mount_ro','unmount','eject','refresh'):
        return jsonify(ok=False,error='Unsupported control'),400
    if action=='refresh':return jsonify(status_data())
    if not lock.acquire(False):return jsonify(ok=False,error='Another tape operation is active'),409
    try:
        if job.get('state')=='running':raise RuntimeError('A tape transfer is currently active')
        update(state='running',action=action,message='Running '+action)
        if action=='mount_ro':
            if mode()=='unmounted':inquiry();REFRESH_CACHE['time']=time.monotonic()
            try:mount_aurora(False)
            except Exception as e:
                c=aurora()
                logs=c.logs(tail=70).decode('utf-8','replace') if c else ''
                message=str(e)+'\n'+logs[-4000:]
                record_mount_failure(message)
                raise RuntimeError('Mount failed: '+('Possible blank/unformatted tape (EOD not found)' if 'EOD not found' in message else str(e)))
            try:LAST_MOUNT_FAILURE.unlink(missing_ok=True)
            except OSError:pass
        elif action in ('unmount','eject'):
            busy=busy_mount_users()
            if busy:raise RuntimeError('Cannot '+action+': tape is busy. '+busy)
            stop_aurora()
            if action=='eject':
                r=command(['mt','-f',NST_DEVICE,'offline'],timeout=120)
                if not r['ok']:raise RuntimeError('Eject failed: '+r['output'])
                REFRESH_CACHE['presence']='empty'
                REFRESH_CACHE['presence_time']=time.monotonic()
            if mounts():restart_gui_later()
        update(state='idle',action='',message='Ready')
        if action!='eject':REFRESH_CACHE['presence_time']=0
        return jsonify(ok=True,message={'mount_ro':'Mounted LTFS read-only',
            'unmount':'Unmounted LTFS (refreshing mount namespace if needed)',
            'eject':'Eject command completed (refreshing mount namespace if needed)'}[action])
    except Exception as e:
        update(state='idle',action='',message='Last control failed: '+str(e))
        return jsonify(ok=False,error=str(e)),409
    finally:lock.release()

@app.get('/api/source')
def source_browse():
    try:
        relative=request.args.get('path','');p=safe_path(SRC,relative)
        if not p.is_dir():raise ValueError('Not a directory')
        entries=[{'name':x.name,'path':str(x.relative_to(SRC)),'dir':x.is_dir(),'bytes':x.stat().st_size if x.is_file() else None}
                 for x in sorted(p.iterdir(),key=lambda x:(not x.is_dir(),x.name.lower())) if not x.is_symlink()]
        return jsonify(ok=True,entries=entries,path=relative)
    except (OSError,ValueError) as e:return jsonify(ok=False,error=str(e)),400

@app.post('/api/estimate')
def estimate():
    data=request.get_json(silent=True) or {};base=SRC if data.get('kind')=='write' else TAPE
    if base==TAPE and mode()=='unmounted':return jsonify(ok=False,error='Tape not mounted'),409
    try:
        selected=data.get('paths',[])
        if not isinstance(selected,list) or len(selected)>10000:raise ValueError('Invalid selection')
        seen=set();total=0;count=0
        for rel in selected:
            if not isinstance(rel,str):raise ValueError('Invalid path')
            p=safe_path(base,rel)
            if p.is_symlink() or not p.exists():raise ValueError('Invalid selection: '+rel)
            items=files_under(p) if p.is_dir() else [(p,Path(p.name),p.stat().st_size)]
            for file,_,size in items:
                if file not in seen:seen.add(file);total+=size;count+=1
        free=None
        if mode()!='unmounted':
            st=os.statvfs(TAPE);free=st.f_bavail*st.f_frsize
        if base==TAPE:
            space=destination_space()
            return jsonify(ok=True,bytes=total,files=count,free=space['free'],
                reserve=space['reserve'],usable=space['usable'],estimated_fit=total<=space['usable'])
        return jsonify(ok=True,bytes=total,files=count,free=free,estimated_fit=None if free is None else total+1024**3<=free)
    except (OSError,ValueError) as e:return jsonify(ok=False,error=str(e)),400

@app.get('/api/browse')
def browse():
    if mode()=='unmounted':return jsonify(ok=False,error='Tape not mounted'),409
    try:
        relative=request.args.get('path','');p=safe_path(TAPE,relative)
        if not p.is_dir():raise ValueError('Not a directory')
        entries=[{'name':x.name,'path':str(x.relative_to(TAPE)),'dir':x.is_dir(),'bytes':x.stat().st_size if x.is_file() else None}
                 for x in sorted(p.iterdir(),key=lambda x:(not x.is_dir(),x.name.lower())) if not x.is_symlink()]
        return jsonify(ok=True,entries=entries,path=relative)
    except (OSError,ValueError) as e:return jsonify(ok=False,error=str(e)),400
@app.get('/api/format-info')
def format_info():
    if lock.locked():return jsonify(ok=False,error='Tape operation in progress'),409
    if mode()!='unmounted':
        return jsonify(ok=True,presence='inserted',mount=mode(),warning='Tape is mounted. Unmount before formatting.',mount_failure=mount_failure())
    presence=drive_presence()
    return jsonify(ok=True,presence=presence,mount='unmounted',mount_failure=mount_failure(),
        warning='Cartridge data and existing LTFS identity cannot be guaranteed from this check. Formatting destroys all tape contents.')

@app.post('/api/format')
def format_start():
    data=request.get_json(silent=True) or {}
    label=data.get('label','')
    if not isinstance(label,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',label):
        return jsonify(ok=False,error='Volume name must be 1–32 letters, digits, underscore or hyphen'),400
    tape_serial=data.get('tape_serial','')
    if not isinstance(tape_serial,str) or (tape_serial and not re.fullmatch(r'[A-Za-z0-9]{6}',tape_serial)):
        return jsonify(ok=False,error='Tape serial must be six alphanumeric characters or blank'),400
    if data.get('confirmed') is not True:return jsonify(ok=False,error='Both confirmations required'),400
    if not lock.acquire(False):return jsonify(ok=False,error='Tape operation in progress'),409
    try:
        if mode()!='unmounted':raise RuntimeError('Unmount the tape before formatting')
        if busy_mount_users():raise RuntimeError('Tape is busy')
        if drive_presence()!='inserted':raise RuntimeError('Cannot confirm cartridge present')
        update(state='running',action='format',message='Starting format',done=0,total=0)
        threading.Thread(target=worker,args=('format',{'label':label,'tape_serial':tape_serial}),daemon=True).start()
        return jsonify(ok=True,message='Formatting started. Do not interrupt.'),202
    except Exception as e:
        lock.release();return jsonify(ok=False,error=str(e)),409

def read_plan(paths):
    if not isinstance(paths,list) or not paths or len(paths)>10000:
        raise ValueError('Select at least one file or folder')
    # Deduplicate selected descendants and preserve full paths from tape root.
    selected=[]
    for rel in paths:
        if not isinstance(rel,str) or not rel or rel.startswith('/') or '\\' in rel:
            raise ValueError('Invalid selected path')
        p=safe_path(TAPE,rel)
        if p.is_symlink() or not p.exists():raise ValueError('Selected item missing or symlink: '+rel)
        selected.append(p)
    files={}
    for p in selected:
        if p.is_file():files[str(p.relative_to(TAPE))]=(p,p.stat().st_size)
        elif p.is_dir():
            for f,_,size in files_under(p):files[str(f.relative_to(TAPE))]=(f,size)
        else:raise ValueError('Unsupported tape item')
    return [(rel,*files[rel]) for rel in sorted(files)]

# Write safety: preflight is repeated under the operation lock at start.
def write_plan(paths,all_files=False):
    if all_files:paths=[p.name for p in SRC.iterdir() if not p.is_symlink()]
    if not isinstance(paths,list) or not paths or len(paths)>10000:raise ValueError('Select at least one file or folder')
    found={}
    for rel in paths:
        if not isinstance(rel,str) or not rel or rel.startswith('/') or '\\' in rel:raise ValueError('Invalid source path')
        p=safe_path(SRC,rel)
        if p.is_symlink() or not p.exists():raise ValueError('Missing or symlink source: '+rel)
        if p.is_file():found[str(p.relative_to(SRC))]=(p,p.stat().st_size)
        elif p.is_dir():
            for f,_,size in files_under(p):found[str(f.relative_to(SRC))]=(f,size)
        else:raise ValueError('Unsupported source')
    if not found:raise ValueError('No files in selection')
    return [(rel,*found[rel]) for rel in sorted(found)]

def aurora_exec(args,timeout=None):
    c=aurora()
    if not c or c.status!='running':raise RuntimeError('Aurora container stopped')
    result=c.exec_run(args,demux=False)
    output=(result.output or b'').decode('utf-8','replace')
    if result.exit_code!=0:raise RuntimeError('Aurora command failed: '+output[-600:])
    return output.strip()

def tape_exists(rel):
    # On read-only preflight, GUI's /tape is known valid after initial mount.
    p=safe_path(TAPE,rel)
    return p.exists() or p.is_symlink()

def write_conflicts(plan):
    return [rel for rel,_,_ in plan if tape_exists(rel)]

@app.post('/api/write-preview')
def write_preview():
    if lock.locked():return jsonify(ok=False,error='Tape operation in progress'),409
    if mode()!='ro':return jsonify(ok=False,error='Mount tape read-only first'),409
    try:
        data=request.get_json(silent=True) or {}
        plan=write_plan(data.get('paths',[]),data.get('all') is True)
        conflicts=write_conflicts(plan)
        st=os.statvfs(TAPE);free=st.f_bavail*st.f_frsize
        return jsonify(ok=True,files=len(plan),bytes=sum(x[2] for x in plan),free=free,conflicts=conflicts,plan=[{"path":rel,"bytes":size} for rel,_,size in plan],
                       warning='Tape capacity is an estimate; LTFS metadata and compression can affect actual free space.')
    except (ValueError,OSError) as e:return jsonify(ok=False,error=str(e)),400

def unique_tape_name(rel,reserved):
    path=Path(rel)
    for n in range(1,10001):
        candidate=str(path.with_name(path.stem+f' ({n})'+path.suffix))
        if candidate not in reserved and not tape_exists(candidate):return candidate
    raise RuntimeError('Cannot generate unique name: '+rel)

@app.post('/api/write-start')
def write_start():
    data=request.get_json(silent=True) or {}
    if data.get('confirmed') is not True or data.get('confirmed_again') is not True:
        return jsonify(ok=False,error='Two confirmations required'),400
    if not lock.acquire(False):return jsonify(ok=False,error='Tape operation in progress'),409
    try:
        if job.get('state')=='running':raise RuntimeError('Another operation is running')
        if mode()!='ro':raise RuntimeError('Mount LTFS read-only first')
        busy=busy_mount_users()
        if busy:raise RuntimeError('Tape is busy: '+str(busy))
        aurora_exec(['sh','-c','command -v sha256sum >/dev/null && command -v cat >/dev/null && command -v mkdir >/dev/null && command -v dirname >/dev/null'])
        plan=write_plan(data.get('paths',[]),data.get('all') is True)
        conflicts=write_conflicts(plan)
        resolutions=data.get('resolutions',{})
        if not isinstance(resolutions,dict):raise ValueError('Invalid duplicate resolutions')
        if any(x not in ('skip','keep_both','stop') for x in resolutions.values()):raise ValueError('Invalid duplicate resolution')
        if any(rel not in resolutions for rel in conflicts):raise ValueError('All duplicates must be resolved before writing')
        if any(resolutions[rel]=='stop' for rel in conflicts):raise ValueError('Stopped by duplicate policy')
        reserved=set(rel for rel,_,_ in plan)
        effective=[]
        for rel,p,size in plan:
            if rel in conflicts:
                if resolutions[rel]=='skip':continue
                target=unique_tape_name(rel,reserved);reserved.add(target)
            else:target=rel
            effective.append((rel,p,size,target))
        if not effective:raise ValueError('No new files to write')
        total=sum(x[2] for x in effective)
        st=os.statvfs(TAPE);free=st.f_bavail*st.f_frsize
        if total+1024**3>free:raise RuntimeError('Insufficient reported tape free space including 1 GiB safety margin')
        # Freeze size and mtime: detect source changes before copying each file.
        frozen=[(rel,p,size,target,p.stat().st_mtime_ns) for rel,p,size,target in effective]
        update(state='running',action='write',stage='preparing',file='',done=0,total=len(frozen),verified=0,
               bytes_done=0,bytes_total=total,transfer_started_at=None,transfer_finished_at=None,finished_at=None,mismatches=0,verification_error='',message='Starting guarded LTFS write',
               started_at=datetime.now(timezone.utc).isoformat())
        threading.Thread(target=write_worker,args=(frozen,),daemon=True).start()
        return jsonify(ok=True,files=len(frozen),bytes=total,message='Write started; do not interrupt or eject'),202
    except Exception as e:
        lock.release();return jsonify(ok=False,error=str(e)),409

def copy_to_tape_with_progress(src, dst, base_bytes, file_size):
    """Copy with exclusive creation, reporting source bytes consumed by cat.

    Progress is informational; only a successful copy plus SHA-256 counts as written.
    No tape-side metadata queries are performed during streaming.
    """
    token=uuid.uuid4().hex
    progress='/tmp/ltfs-write-progress-'+token
    finished=threading.Event()
    def monitor():
        last=0
        while not finished.wait(2):
            try:
                raw=aurora_exec(['cat',progress]).strip()
                current=min(file_size,max(last,int(raw)))
                last=current
                update(bytes_done=base_bytes+current)
            except (ValueError,RuntimeError,IndexError):
                pass
    # Preserve exclusive create (set -C), original cat transfer and error propagation.
    # Linux /proc/PID/io rchar counts bytes consumed from the source; it may lead
    # physical tape persistence. Commit and SHA-256 remain the authoritative checks.
    script='''set -eu
mkdir -p -- "$(dirname -- "$2")"
set -C
cat -- "$1" > "$2" &
pid=$!
trap 'rm -f -- "$3"' EXIT
while kill -0 "$pid" 2>/dev/null; do
  if [ -r "/proc/$pid/io" ]; then
    awk '/^rchar:/ {print $2}' "/proc/$pid/io" >| "$3" 2>/dev/null || true
  fi
  sleep 2
done
wait "$pid"
'''
    t=threading.Thread(target=monitor,daemon=True)
    t.start()
    try:
        aurora_exec(['sh','-c',script,'ltfs-write',src,dst,progress])
    finally:
        finished.set()
        t.join(timeout=4)

def write_worker(plan):
    wrote=[]
    try:
        # Important: GUI /tape mount namespace can become stale across remounts.
        # All tape I/O is executed INSIDE the Aurora container, never through GUI /tape.
        update(stage='mounting',message='Switching LTFS to read-write; do not interrupt')
        mount_aurora(True,allow_stale=True)
        if mode()!='rw':raise RuntimeError('Read-write mount not confirmed')
        for i,(rel,p,size,target,mtime) in enumerate(plan):
            if p.stat().st_size!=size or p.stat().st_mtime_ns!=mtime:raise RuntimeError('Source file changed before writing: '+rel)
            # All paths originate under /source, and target is an LTFS-root-relative path.
            src='/source/'+rel;dst='/ltfs/'+target
            update(transfer_started_at=job.get('transfer_started_at') or datetime.now(timezone.utc).isoformat(),stage='writing',file=target,message=f'Writing {i+1}/{len(plan)}: '+target)
            # Exclusive creation is retained; live progress is informational.
            base_bytes=sum(x[2] for x in wrote)
            copy_to_tape_with_progress(src,dst,base_bytes,size)
            # Compare tape copy against source before index commit, fail on mismatch.
            expected=hash_file(p)
            actual=aurora_exec(['sha256sum','--',dst]).split()[0]
            if expected!=actual:raise RuntimeError('SHA-256 mismatch before commit: '+target)
            if p.stat().st_size!=size or p.stat().st_mtime_ns!=mtime:raise RuntimeError('Source changed while writing: '+rel)
            wrote.append((rel,p,size,target,expected))
            update(done=i+1,bytes_done=sum(x[2] for x in wrote))
        update(transfer_finished_at=datetime.now(timezone.utc).isoformat(),stage='committing',message='Committing LTFS index; do not interrupt')
        stop_aurora() # Clean stop is required for sync_type=unmount.
        update(stage='remounting',message='Mounting tape read-only for independent verification')
        mount_aurora(False,allow_stale=True)
        if mode()!='ro':raise RuntimeError('Read-only remount not confirmed')
        for i,(rel,p,size,target,expected) in enumerate(wrote):
            update(stage='verifying',file=target,message=f'Verifying committed tape file {i+1}/{len(wrote)}')
            if hash_file(p)!=expected:raise RuntimeError('Source changed during verification: '+rel)
            actual=aurora_exec(['sha256sum','--','/ltfs/'+target]).split()[0]
            if actual!=expected:raise RuntimeError('SHA-256 mismatch after read-only remount: '+target)
            update(verified=i+1)
        update(finished_at=datetime.now(timezone.utc).isoformat(),stage='completed',state='completed',file='',message=f'Write committed and SHA-256 verified: {len(wrote)} files')
    except Exception as e:
        update(stage='failed',state='failed',verification_error=str(e),message='Write stopped: '+str(e))
        # Do not automatically retry writes or pretend an incomplete commit succeeded.
        # If mounted RW, attempt clean commit, then RO remount for inspection.
        try:
            if mode()=='rw':
                update(message='Write failed; attempting safe LTFS index commit (do not eject)')
                stop_aurora()
                mount_aurora(False,allow_stale=True)
                update(message='Write failed; LTFS remounted read-only. Inspect partial files: '+str(e))
        except Exception as recovery:
            update(message='Write failed and safe recovery failed. DO NOT EJECT. '+str(e)+'; recovery: '+str(recovery))
    finally:
        lock.release()
        # GUI mount namespace may be stale. Restart only after final operation state is saved.
        if mode()=='ro':restart_gui_later()

@app.post('/api/read-preview')
def read_preview():
    if lock.locked():return jsonify(ok=False,error='Tape operation in progress'),409
    try:
        if mode()!='ro':raise RuntimeError('Mount LTFS read-only before restoring files')
        data=request.get_json(silent=True) or {}
        paths=data.get('paths',[])
        if data.get('all') is True:paths=[p.name for p in TAPE.iterdir() if not p.is_symlink()]
        plan=read_plan(paths)
        if not plan:raise RuntimeError('No files selected for restore')
        total=sum(size for _,_,size in plan)
        space=destination_space()
        return jsonify(ok=True,files=len(plan),bytes=total,**space,estimated_fit=total<=space['usable'])
    except (OSError,ValueError,RuntimeError) as e:return jsonify(ok=False,error=str(e)),409

@app.post('/api/read-start')
def read_start():
    data=request.get_json(silent=True) or {}
    if not lock.acquire(False):return jsonify(ok=False,error='Another tape operation is active'),409
    try:
        if job.get('state')=='running':raise RuntimeError('Another tape operation is active')
        if mode()!='ro':raise RuntimeError('Mount LTFS read-only before restoring files')
        paths=data.get('paths',[])
        if data.get('all') is True:
            paths=[p.name for p in TAPE.iterdir() if not p.is_symlink()]
        plan=read_plan(paths)
        if not plan:raise RuntimeError('No files found to restore')
        # Unique destination folder avoids overwriting any existing restore.
        out=DEST/('LTFS_RESTORE_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
        if out.exists():raise RuntimeError('Restore destination already exists')
        total=sum(size for _,_,size in plan)
        require_destination_space(total,'preflight')
        update(state='running',action='read',stage='copying',verified=0,mismatches=0,verification_error='',message='Starting read-only restore',file='',done=0,total=len(plan),bytes_done=0,bytes_total=total,transfer_started_at=None,transfer_finished_at=None,finished_at=None,started_at=datetime.now(timezone.utc).isoformat(),destination=str(out))
        threading.Thread(target=read_worker,args=(plan,out),daemon=True).start()
        return jsonify(ok=True,message='Read-only restore started',destination=str(out),files=len(plan),bytes=total),202
    except Exception as e:
        lock.release();return jsonify(ok=False,error=str(e)),409

def read_worker(plan,out):
    try:
        require_destination_space(sum(size for _,_,size in plan),'restore start')
        out.mkdir(parents=True,exist_ok=False)
        for index,(rel,src,size) in enumerate(plan):
            if mode()!='ro':raise RuntimeError('LTFS mount lost during restore')
            require_destination_space(sum(item[2] for item in plan[index:]),'before next file')
            dst=safe_path(out,rel)
            # Never overwrite; ensure destination paths are not symlinks.
            dst.parent.mkdir(parents=True,exist_ok=True)
            if any(x.is_symlink() for x in [dst,*dst.parents] if x!=Path('/')):
                raise RuntimeError('Unsafe destination symlink: '+rel)
            update(transfer_started_at=job.get('transfer_started_at') or datetime.now(timezone.utc).isoformat(),stage='copying',file=rel,message='Reading file '+str(index+1)+'/'+str(len(plan)))
            source_hash=hashlib.sha256()
            try:
                with open(src,'rb') as reader,open(dst,'xb') as writer:
                    while True:
                        chunk=reader.read(4*1024*1024)
                        if not chunk:break
                        require_destination_space(len(chunk),'during copy')
                        writer.write(chunk);source_hash.update(chunk)
                        update(bytes_done=job['bytes_done']+len(chunk))
                update(transfer_finished_at=datetime.now(timezone.utc).isoformat(),stage='verifying',message='Verifying restored file '+str(index+1)+'/'+str(len(plan)))
                if hash_file(dst)!=source_hash.hexdigest():
                    raise RuntimeError('SHA-256 destination verification failed: '+rel)
                # Read tape source again to independently verify restored bytes.
                if hash_file(src)!=source_hash.hexdigest():
                    raise RuntimeError('SHA-256 tape re-read verification failed: '+rel)
            except Exception:
                # Preserve partial file for investigation; do not silently retry.
                raise
            update(done=index+1,verified=index+1)
        update(finished_at=datetime.now(timezone.utc).isoformat(),stage='completed',state='completed',message='Restore complete: '+str(len(plan))+' files verified with SHA-256 in '+str(out),file='')
    except Exception as e:
        update(stage='failed',state='failed',verification_error=str(e),mismatches=1 if 'SHA-256' in str(e) else 0,message='Restore stopped: '+str(e))
    finally:
        lock.release()

if __name__=='__main__':
    if (STATE/'operation.json').exists():
        try:
            old=json.loads((STATE/'operation.json').read_text())
            if old.get('state')=='running':old.update(state='interrupted',message='GUI restarted during operation; inspect tape before retrying')
            job.update(old)
        except Exception:pass
    app.run(host='0.0.0.0',port=8080,threaded=True)
