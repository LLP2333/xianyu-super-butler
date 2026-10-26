import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import AccountBrowser from '../components/AccountBrowser';
import '../index.css';

function Harness() {
  const [open, setOpen] = useState(false);
  return <><button onClick={() => setOpen(true)}>打开测试账号</button>{open && <AccountBrowser
    account={{ id: 'a', nickname: '远程桌面测试账号', enabled: true, auto_confirm: false, pause_duration: 0 }}
    onClose={() => setOpen(false)} onChanged={() => {}} />}</>;
}
createRoot(document.getElementById('root')!).render(<Harness />);
