'''
This software creates a virtual sink in pulseaudio that captures
all system audio output and inputs in a single recordable stream.

The program keeps running monitoring and autoconnecting new audio
inputs or outputs to the stream as they become available.

Usage example with SimpleScreenRecorder (SSR):
  1. run python3 caphVSink.py
  2. select caphVSink in the audio combo-box of SSR and record
  You will be recording mic and speakers.

Use pavucontrol if you are curious and want to monitor caphVSinc.

Author: Carlos Pinzón - caph1993@gmail.com
'''
import pulsectl
import re
import time
from collections import deque
from threading import Thread


class MyPulse(pulsectl.Pulse):
    '''
    Wrapper that simplifies handling virtual sink and loopbacks
    Uses sink and sources names (strings) to index (avoid repeated names!)
    '''
    
    def __init__(self):
        super().__init__('MyPulse')
    
    def module_items(self, name):
        'list modules as tuples (parsed_args_dict, module)'
        E = []
        for mod in self.module_list():
            if mod.name==name:
                args = dict(re.findall(r'([^ ]*?)=([^ ]*)', mod.argument))
                E.append((args, mod))
        return E
    
    def vsink_dict(self):
        'index existing virtual sinks as {name: module}'
        items = self.module_items('module-null-sink')
        return {e.get('sink_name'): mod for e, mod in items}

    def loopback_dict(self):
        'index existing loopback echos as {(source, sink): module}'
        items = self.module_items('module-loopback')
        return {(e.get('source'), e.get('sink')): mod for e, mod in items}
    
    def vsource_dict(self):
        'index existing virtual sources as {name: module}'
        items = self.module_items('module-null-source')
        return {e.get('source_name'): mod for e, mod in items}
    
    def vsink_add(self, name):
        'create a virtual sink if it does not exist. returns index'
        curr = self.vsink_dict().get(name)
        return curr.index if curr else self._vsink_add(name)
    
    def _vsink_add(self, name):
        args = f'sink_name={name} sink_properties=device.description={name}'
        return self.module_load('module-null-sink', args=args)
    
    def loopback_add(self, source, sink):
        'create a virtual sink if it does not exist. returns index'
        curr = self.loopback_dict().get((source, sink))
        return curr.index if curr else self._loopback_add(source, sink)
    
    def _loopback_add(self, source, sink):
        return self.module_load('module-loopback', args=f'source={source} sink={sink}')
    
    def vsink_disconnect(self, sink):
        'removes all connections to a virtual sink if it exists'
        for (src,tgt), mod in self.loopback_dict().items():
            if tgt == sink:
                self.module_unload(mod.index)
        return
    
    def vsink_remove(self, name):
        'disconnects and removes a virtual sink if it exists'
        self.vsink_disconnect(name)
        for e, mod in self.module_items('module-null-sink'):
            if e.get('sink_name')==name:
                self.module_unload(mod.index)
        return
    
    def loopback_remove(self, source, sink):
        'disconnects source and sink if they are connected with a loopback module'
        for (src,tgt), mod in self.loopback_dict().items():
            if (src,tgt)==(source,sink):
                self.module_unload(mod.index)
        return
    
    def vsink_sources(self, name):
        'get all sources captured by the virtual sink'
        return {src:mod for (src, tgt), mod in self.loopback_dict().items() if tgt==name}
    
    def vsink_apps(self, name):
        'get all apps using the virtual sink'
        idx = self.vsink_source(name)
        apps = {c for c in self.source_output_list() if c.source==idx}
        is_pavucontrol = lambda c: c.name == 'Peak detect'
        return {c.name: c for c in apps if not is_pavucontrol(c)}
    
    def vsink_source(self, name):
        'get index of the monitor of virtual sink, i.e. the source'
        for src in self.source_list():
            if src.name == f'{name}.monitor':
                return src.index
        return -1
    
    def vsink_safe_remove(self, name):
        'disconnects and removes a virtual sink if it exists and no app is using it'
        apps = self.vsink_apps(name)
        if not apps:
            self.vsink_remove(name)
        return apps
    
class PulseEventsListener(pulsectl.Pulse):
    events = deque()
    _stop = False
    _done = False
    
    def __init__(self, client_name='events-listener'):
        super().__init__(client_name)
        Thread(target=self._listen).start()
    
    def _listen(self):
        def hook(event):
            self.events.append(event)
            if self._stop:
                raise pulsectl.PulseLoopStop
        self.event_mask_set('all')
        self.event_callback_set(hook)
        self.event_listen(timeout=0)
        self._done = True
    
    def stop(self):
        self._stop = True


class Capturer(MyPulse):
    
    def __init__(self, name='caphVSink', delta=0.5):
        super().__init__()
        self.vsink_add(name)
        self.capturer_connect(name)
        self.srcs = set()
        self.apps = set()
        listener = PulseEventsListener(f'{name}-events')
        print(f'You may now use [{name}] as audio source. Quit with ctrl-C.', flush=True)
        print('\nConnections:')
        self.capturer_refresh(name)
        try:
            while 1:
                if listener.events:
                    listener.events.clear()
                    self.capturer_refresh(name)
                else:
                    time.sleep(delta)
        except KeyboardInterrupt:
            print('...ending...', flush=True)
            listener.stop()
            apps = self.vsink_safe_remove(name)
            if not apps:
                print(f'Connection killed')
                bye = '\nFinished'
            else:
                print(f'Some apps are still using this connection:\n')
                print(*(f'  {app}' for app in apps), sep='\n')
                bye = '\nConnection left alive'
        finally:
            self.close()
            print(bye)
        
    def capturer_refresh(self, name):
        srcs = self.srcs
        apps = self.apps
        self.capturer_connect(name)
        self.srcs = set(self.vsink_sources(name))
        self.apps = set(self.vsink_apps(name))
        self.capturer_deltaprint(srcs, apps)
        
    def capturer_deltaprint(self, old_srcs, old_apps, prefix='  '):
        for src in self.srcs - old_srcs:
            print(f'{prefix}Connected source    {src}')
        for src in old_srcs - self.srcs:
            print(f'{prefix}Disconnected source {src}')
        for app in self.apps - old_apps:
            print(f'{prefix}Connected app       {app}')
        for app in old_apps - self.apps:
            print(f'{prefix}Disconnected app    {app}')
        
    def capturer_connect(self, name):
        'connect all existing sources to the given virtual sink'
        for src in self.source_list():
            if src.name != f'{name}.monitor':
                self.loopback_add(src.name, name)
        return


if __name__=='__main__':
    Capturer('caphVSink')
