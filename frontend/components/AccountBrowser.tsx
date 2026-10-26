import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import RFB from '@novnc/novnc';
import { accountBrowserAction, accountBrowserTicket } from '../services/api';
import { AccountDetail } from '../types';

const errorMessage = (error: unknown) => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' ? detail : error instanceof Error ? error.message : '操作失败';
};

type Props = { account: AccountDetail; onClose: () => void; onChanged: () => void };

export default function AccountBrowser({ account, onClose, onChanged }: Props) {
  const screen = useRef<HTMLDivElement>(null);
  const expectedDisconnect = useRef(false);
  const connection = useRef<RFB | null>(null);
  const [message, setMessage] = useState('正在打开账号浏览器…');
  const [busy, setBusy] = useState(true);
  const [manual, setManual] = useState(false);
  const [connected, setConnected] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let rfb: RFB | null = null;
    const open = async () => {
      expectedDisconnect.current = false;
      setBusy(true);
      setConnected(false);
      try {
        const result = await accountBrowserAction(account.id, 'open');
        if (cancelled) return;
        setManual(true);
        setMessage(result.message);
        onChanged();
        const { ticket } = await accountBrowserTicket(account.id);
        if (cancelled || !screen.current) return;
        const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
        rfb = new RFB(screen.current,
          `${scheme}://${location.host}/api/account-browser/${encodeURIComponent(account.id)}/desktop?ticket=${encodeURIComponent(ticket)}`);
        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.addEventListener('connect', () => { if (!cancelled) setConnected(true); });
        rfb.addEventListener('disconnect', () => {
          if (!cancelled) {
            setConnected(false);
            if (!expectedDisconnect.current) setMessage('远程连接已断开，浏览器和接管状态仍保留。可点击“重新连接”。');
          }
        });
        connection.current = rfb;
      } catch (error) {
        if (!cancelled) setMessage(errorMessage(error));
      } finally {
        if (!cancelled) setBusy(false);
      }
    };
    void open();
    return () => {
      cancelled = true;
      rfb?.disconnect();
      if (connection.current === rfb) connection.current = null;
    };
    // onChanged refreshes the list; it must not recreate the remote connection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account.id, attempt]);

  const act = async (action: 'challenge' | 'confirm' | 'release') => {
    setBusy(true);
    if (action !== 'challenge') {
      expectedDisconnect.current = true;
      connection.current?.disconnect();
    }
    try {
      const result = await accountBrowserAction(account.id, action);
      setMessage(result.message);
      if (action !== 'challenge') {
        setManual(false);
        onChanged();
      }
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return createPortal(
    <div className="modal-overlay">
      <div className="modal-container" style={{ maxWidth: '1440px', width: '96vw' }}>
        <div className="modal-header flex items-center justify-between gap-3">
          <div>
            <h3 className="text-lg font-bold">账号浏览器 · {account.nickname || account.remark || account.id}</h3>
            <p className="text-sm text-gray-500">{connected ? '已连接远程桌面' : '远程桌面未连接'} · 关闭窗口保留浏览器；接管期间自动任务暂停</p>
          </div>
          <button type="button" className="ios-btn-secondary px-3 py-2" onClick={onClose} disabled={busy}>关闭窗口</button>
        </div>
        <div className="px-4 py-2 text-sm bg-amber-50 text-amber-900" role="status">{message}</div>
        <div ref={screen} style={{ height: '65vh', minHeight: 320, background: '#202020', overflow: 'hidden' }} />
        <div className="modal-footer flex flex-wrap gap-2">
          <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy} onClick={() => setAttempt(n => n + 1)}>重新连接</button>
          <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy || !manual || !connected} onClick={() => act('challenge')}>打开验证页</button>
          <button type="button" className="ios-btn-secondary px-3 py-2" disabled={busy || !manual} onClick={() => act('release')}>不保存并结束接管</button>
          <button type="button" className="ios-btn-primary px-3 py-2" disabled={busy || !manual} onClick={() => act('confirm')}>确认登录并恢复</button>
        </div>
      </div>
    </div>, document.body,
  );
}
