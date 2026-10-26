import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { accountBrowserAction, accountBrowserStatus, BrowserDesktop, BrowserStatus, setupBrowserAccount,
  browserSetupStatus, browserDesktopLink, clearBrowserData, reconnectBrowserListener } from '../services/api';
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
  const [status, setStatus] = useState<BrowserStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [reconnecting, setReconnecting] = useState(false);
  const [createdId, setCreatedId] = useState<string>();
  const accountId = account?.id || createdId;
  const windowRef = useRef(initialWindow);
  const previousState = useRef<string>();

  useEffect(() => {
    let cancelled = false;
    const request = account ? accountBrowserAction(account.id, 'open') : setupBrowserAccount();
    void request.then(result => {
      if (cancelled) return;
      setDesktop(result); setMessage(result.message);
      if (result.open_url && windowRef.current && !windowRef.current.closed) {
        windowRef.current.location.href = result.open_url;
        windowRef.current = null;
      }
      onChanged();
    }).catch(error => {
      if (!cancelled) { setMessage(errorMessage(error)); setStatusError(errorMessage(error)); }
      windowRef.current?.close(); windowRef.current = null;
    }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
    // List refreshes must not reopen the browser or restart the account lifecycle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account?.id]);

  useEffect(() => {
    if (busy) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = accountId ? await accountBrowserStatus(accountId) : await browserSetupStatus();
        if (cancelled) return;
        setStatus(result); setStatusError(null);
        if (result.account_id) setCreatedId(result.account_id);
        const current = `${result.account_id || accountId}:${result.credentials_state}`;
        if (current !== previousState.current) { previousState.current = current; onChanged(); }
      } catch (error) {
        if (!cancelled) setStatusError(errorMessage(error));
      }
      if (!cancelled) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, busy]);

  const reconnect = async () => {
    setBusy(true); setReconnecting(true); setStatusError(null);
    try {
      const result = desktop ? await reconnectBrowserListener()
        : accountId ? await accountBrowserAction(accountId, 'open') : await setupBrowserAccount();
      setDesktop(result); setMessage(result.message); setStatus(null);
    } catch (error) { setStatusError(errorMessage(error)); }
    finally { setBusy(false); setReconnecting(false); }
  };

  const openDesktop = async () => {
    const opened = window.open('about:blank', '_blank');
    if (opened) opened.opener = null;
    try {
      const { open_url } = await browserDesktopLink();
      if (opened) opened.location.href = open_url;
      else setMessage('浏览器阻止了新窗口，请允许此网站打开新窗口后重试。');
    } catch (error) { opened?.close(); setMessage(errorMessage(error)); }
  };

  const act = async (action: 'restart' | 'clear') => {
    if (action === 'clear' && !await confirmAction('清空浏览器的登录记录、Cookie、缓存及本地数据？账号设置保留，自动任务将暂停，需要重新登录。')) return;
    setBusy(true); setStatusError(null);
    try {
      const result = action === 'clear' ? await clearBrowserData() : await accountBrowserAction(accountId!, action);
      setDesktop(result); setMessage(result.message); setStatus(null); onChanged();
    } catch (error) { setStatusError(errorMessage(error)); }
    finally { setBusy(false); }
  };

  const listening = !busy && !statusError && status?.listener_state === 'listening';
  const active = !busy && !statusError && status?.credentials_state === 'active';
  const titles = { waiting: '等待网页登录并捕获凭据', captured: '已捕获 Cookie 和消息 Token，正在自动采用',
    active: createdId ? '✓ 账号已添加，Cookie 和消息 Token 已采用' : '✓ Cookie 已保存，消息 Token 已采用', error: '凭据采用失败，账号保持暂停' };

  return createPortal(
    <div className="modal-overlay">
      <div className="modal-container" style={{ maxWidth: 680, width: '96vw' }}>
        <div className="modal-header flex items-center justify-between gap-3">
          <h3 className="text-lg font-bold">{account ? `原生浏览器 · ${account.nickname || account.id}` : '浏览器添加账号'}</h3>
          <button type="button" className="ios-btn-secondary px-3 py-2" onClick={onClose}>关闭窗口</button>
        </div>
        <div className="p-5 space-y-4">
          <p role="status">{message}</p>
          <div role="status" aria-live="polite" className={`rounded-xl border p-3 ${listening ? 'border-green-200 text-green-800' : 'border-amber-200 text-amber-800'}`}>
            <p className="font-bold">浏览器监听：{busy ? reconnecting ? '重新连接中…' : '正在连接…' : statusError ? '状态查询失败' : status?.listener_state === 'listening' ? '监听中' : status?.listener_state === 'connecting' ? '正在连接…' : '已断开'}</p>
            <p className="text-sm">{statusError || status?.listener_error || (listening ? '监听连接正常，正在接收网页登录结果，无需重新连接。' : '连接异常时可点击“重新连接监听”，无需重启浏览器。')}</p>
          </div>
          <div role="status" aria-live="polite" aria-atomic="true" className={`rounded-xl border p-4 space-y-2 ${active ? 'border-green-200 bg-green-50 text-green-800' : 'border-amber-200 bg-amber-50 text-amber-800'}`}>
            <p className="font-bold">{statusError ? '暂时无法确认凭据状态' : busy || !status ? '正在检查登录凭据…' : titles[status.credentials_state]}</p>
            {status?.captured_at && <p className="text-sm">消息 Token 首次捕获时间：{new Date(status.captured_at * 1000).toLocaleString()}</p>}
            <p className="text-sm">{statusError ? '当前状态无法确认，请等待查询恢复。' : status?.message || '在原生 Chrome 登录闲鱼，然后打开或刷新消息页。'}</p>
            <p className="text-sm">{active ? '账号任务已恢复，实际消息连接结果请查看账号列表。' : '系统每 2 秒刷新状态；捕获有效凭据后会自动保存并恢复，无需手动确认。'}</p>
          </div>
          {desktop?.desktop_url && <>
            <button type="button" className="ios-btn-primary px-4 py-2" disabled={busy} onClick={() => void openDesktop()}>打开原生 Chrome 桌面 ↗</button>
            <p className="text-sm break-all">桌面地址：{desktop.desktop_url}（点击上方按钮自动授权，无需输入桌面密码）</p>
            <p className="text-sm text-gray-500">在 Chrome 内登录闲鱼，然后打开 https://www.goofish.com/im。请使用当前账号，系统自动捕获 Cookie 和消息 Token。</p>
            <p className="text-sm text-gray-500">若出现自签名证书提示，请核对桌面地址。打开链接一分钟内有效，过期后请重新点击按钮。</p>
          </>}
          <p className="text-sm text-gray-500">凭据过期后暂停闲鱼请求。关闭此窗口不会停止监听，也不会提前恢复任务；新凭据采用成功后才恢复。</p>
        </div>
        <div className="modal-footer flex flex-wrap gap-2">
          <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy || listening} onClick={() => void reconnect()}>{reconnecting ? '重新连接中…' : listening ? '监听正常' : '重新连接监听'}</button>
          <button type="button" className="ios-btn-secondary px-3 py-2 text-red-600" disabled={busy || !desktop} onClick={() => void act('clear')}>清空浏览器数据</button>
          {accountId && <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy} onClick={() => void act('restart')}>重启浏览器</button>}
        </div>
      </div>
    </div>, document.body,
  );
}
