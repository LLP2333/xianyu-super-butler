import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { accountBrowserAction, accountBrowserStatus, BrowserDesktop, setupBrowserAccount,
  browserSetupStatus, browserDesktopLink, clearBrowserData } from '../services/api';
import { confirmAction } from '../services/feedback';
import { AccountDetail } from '../types';

const errorMessage = (error: unknown) => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' ? detail : error instanceof Error ? error.message : '操作失败';
};

type Props = { account?: AccountDetail; initialWindow?: Window | null; onClose: () => void; onChanged: () => void };

export default function AccountBrowser({ account, initialWindow, onClose, onChanged }: Props) {
  const [desktop, setDesktop] = useState<BrowserDesktop | null>(null);
  const [message, setMessage] = useState('正在准备原生浏览器…');
  const [status, setStatus] = useState('等待网页消息 Token');
  const [captured, setCaptured] = useState(false);
  const [busy, setBusy] = useState(true);
  const [manual, setManual] = useState(false);
  const [created, setCreated] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const windowRef = useRef(initialWindow);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    const request = account ? accountBrowserAction(account.id, 'open') : setupBrowserAccount();
    void request.then(result => {
      if (cancelled) return;
      setDesktop(result);
      setManual(true);
      setMessage(result.message);
      if (result.open_url && windowRef.current && !windowRef.current.closed) {
        windowRef.current.location.href = result.open_url;
        windowRef.current = null;
      }
      onChanged();
    }).catch(error => {
      if (!cancelled) setMessage(errorMessage(error));
      windowRef.current?.close(); windowRef.current = null;
    }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
    // Parent list refreshes must not recreate the browser lifecycle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account?.id, attempt]);

  useEffect(() => {
    if (!manual || created) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        if (account) {
          const result = await accountBrowserStatus(account.id);
          if (cancelled) return;
          setCaptured(result.captured && result.listening);
          setStatus(result.message);
          setManual(result.manual);
        } else {
          const result = await browserSetupStatus();
          if (cancelled) return;
          setStatus(result.message);
          if (result.account_id) {
            setCreated(true); setManual(false); setMessage(result.message); onChanged();
            return;
          }
        }
      } catch (error) {
        if (!cancelled) { setCaptured(false); setStatus(errorMessage(error)); }
      }
      if (!cancelled) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account?.id, manual, created]);

  const openDesktop = async () => {
    const opened = window.open('about:blank', '_blank');
    if (opened) opened.opener = null;
    try {
      const { open_url } = await browserDesktopLink();
      if (opened) opened.location.href = open_url;
      else setMessage('浏览器阻止了新窗口，请允许此网站打开新窗口后重试。');
    } catch (error) { opened?.close(); setMessage(errorMessage(error)); }
  };

  const act = async (action: 'restart' | 'confirm' | 'release' | 'clear') => {
    if (action === 'clear' && !await confirmAction('清空浏览器的登录记录、Cookie、缓存及本地数据？账号设置保留，自动任务将暂停，需要重新登录。')) return;
    setBusy(true);
    try {
      const result = action === 'clear' ? await clearBrowserData()
        : await accountBrowserAction(account!.id, action);
      setMessage(result.message);
      if (action === 'restart' || action === 'clear') { setDesktop(result); setCaptured(false); setManual(true); }
      else { setManual(false); setCaptured(false); }
      onChanged();
    } catch (error) { setMessage(errorMessage(error)); }
    finally { setBusy(false); }
  };

  return createPortal(
    <div className="modal-overlay">
      <div className="modal-container" style={{ maxWidth: 680, width: '96vw' }}>
        <div className="modal-header flex items-center justify-between gap-3">
          <h3 className="text-lg font-bold">{account ? `原生浏览器 · ${account.nickname || account.id}` : '浏览器添加账号'}</h3>
          <button type="button" className="ios-btn-secondary px-3 py-2" onClick={onClose}>关闭窗口</button>
        </div>
        <div className="p-5 space-y-4">
          <p role="status">{message}</p>
          {manual && <p className="text-amber-700">{status}</p>}
          {desktop?.desktop_url && <>
            <button type="button" className="ios-btn-primary px-4 py-2" disabled={busy} onClick={() => void openDesktop()}>打开原生 Chrome 桌面 ↗</button>
            <p className="text-sm break-all">桌面地址：{desktop.desktop_url}（点击上方按钮自动授权，无需输入桌面密码）</p>
            <p className="text-sm text-gray-500">在 Chrome 内登录闲鱼，然后打开 https://www.goofish.com/im。{account ? '验证完成并捕获 Token 后，回到这里确认。' : '捕获 Cookie 和消息 Token 后，系统会自动添加账号。'}</p>
            <p className="text-sm text-gray-500">若出现自签名证书提示，请核对桌面地址。打开链接一分钟内有效，过期后请重新点击按钮。</p>
          </>}
          <p className="text-sm text-gray-500">{account ? '人工操作期间账号任务暂停，关闭本窗口不会自动恢复。' : '当前先使用一个账号，完成登录后可在账号列表查看连接状态。'}</p>
        </div>
        <div className="modal-footer flex flex-wrap gap-2">
          <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy || created} onClick={() => setAttempt(n => n + 1)}>重新连接监听</button>
          <button type="button" className="ios-btn-secondary px-3 py-2 text-red-600" disabled={busy || created} onClick={() => void act('clear')}>清空浏览器数据</button>
          {account && <>
            <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy || !manual} onClick={() => void act('restart')}>重启浏览器</button>
            <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy || !manual} onClick={() => void act('release')}>结束操作（不更新）</button>
            <button type="button" className="ios-btn-primary px-3 py-2" disabled={busy || !manual || !captured} onClick={() => void act('confirm')}>采用网页 Token 并恢复</button>
          </>}
        </div>
      </div>
    </div>, document.body,
  );
}
