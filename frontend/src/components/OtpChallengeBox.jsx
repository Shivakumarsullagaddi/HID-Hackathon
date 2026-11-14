import React, { useState, useEffect, useRef } from 'react';

/**
 * OtpChallengeBox
 *
 * Standalone, self-contained UI card for multi-factor OTP challenge verification.
 * Accepts:
 *   - challengeId: string (required) - UUID of the pending security challenge
 *   - demoOtpHint: string (optional) - Dev/hackathon demo OTP code hint
 *   - onVerified: function(response) - Callback on successful verification
 *   - onCancel: function() - Callback when user cancels or dismisses
 */
export default function OtpChallengeBox({ challengeId, demoOtpHint, onVerified, onCancel }) {
  const [digits, setDigits] = useState(['', '', '', '', '', '']);
  const [timeLeft, setTimeLeft] = useState(60);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);

  const inputRefs = useRef([]);

  // Countdown timer for 60s TTL
  useEffect(() => {
    if (timeLeft <= 0) return;
    const timer = setInterval(() => {
      setTimeLeft((prev) => {
        if (prev <= 1) {
          clearInterval(timer);
          setError('Challenge expired (60s TTL elapsed). Please request a new verification.');
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [timeLeft]);

  // Auto-focus first input box on mount
  useEffect(() => {
    if (inputRefs.current[0]) {
      inputRefs.current[0].focus();
    }
  }, []);

  // Handle digit typing
  const handleChange = (index, value) => {
    const char = value.replace(/\D/g, '').slice(-1);
    const newDigits = [...digits];
    newDigits[index] = char;
    setDigits(newDigits);
    setError(null);

    // Auto-advance to next input
    if (char && index < 5 && inputRefs.current[index + 1]) {
      inputRefs.current[index + 1].focus();
    }
  };

  // Handle backspace navigation
  const handleKeyDown = (index, e) => {
    if (e.key === 'Backspace') {
      if (!digits[index] && index > 0 && inputRefs.current[index - 1]) {
        inputRefs.current[index - 1].focus();
      }
    } else if (e.key === 'ArrowLeft' && index > 0) {
      inputRefs.current[index - 1].focus();
    } else if (e.key === 'ArrowRight' && index < 5) {
      inputRefs.current[index + 1].focus();
    }
  };

  // Handle paste of full 6-digit code
  const handlePaste = (e) => {
    e.preventDefault();
    const pasteData = e.clipboardData.getData('text').trim().replace(/\D/g, '').slice(0, 6);
    if (!pasteData) return;
    const newDigits = [...digits];
    for (let i = 0; i < pasteData.length; i++) {
      newDigits[i] = pasteData[i];
    }
    setDigits(newDigits);
    const nextIdx = Math.min(pasteData.length, 5);
    if (inputRefs.current[nextIdx]) {
      inputRefs.current[nextIdx].focus();
    }
  };

  // Click to autofill demo OTP
  const handleAutofillDemo = () => {
    if (!demoOtpHint) return;
    const chars = demoOtpHint.slice(0, 6).split('');
    const newDigits = ['', '', '', '', '', ''];
    chars.forEach((c, idx) => {
      newDigits[idx] = c;
    });
    setDigits(newDigits);
    setError(null);
    if (inputRefs.current[5]) {
      inputRefs.current[5].focus();
    }
  };

  const otpCode = digits.join('');
  const isComplete = otpCode.length === 6;

  // Submit OTP to /verify-otp
  const handleSubmit = async (e) => {
    if (e) e.preventDefault();
    if (!isComplete || loading || timeLeft <= 0) return;

    setLoading(true);
    setError(null);

    try {
      const response = await fetch('/verify-otp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          challenge_id: challengeId,
          otp: otpCode,
        }),
      });

      const signatureHeader = response.headers.get('X-Verdict-Signature');
      const data = await response.json();

      if (!response.ok || !data.verified) {
        throw new Error(data.reason || data.detail || 'OTP verification failed');
      }

      setSuccess(true);
      const enrichedResult = {
        ...data,
        signature: data.signature || signatureHeader,
      };

      if (onVerified) {
        setTimeout(() => onVerified(enrichedResult), 600);
      }
    } catch (err) {
      setError(err.message || 'Verification failed. Challenge burned.');
    } finally {
      setLoading(false);
    }
  };

  const formatTimer = (sec) => {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${s < 10 ? '0' : ''}${s}`;
  };

  return (
    <div className="otp-challenge-card" role="region" aria-label="Step-up OTP Challenge">
      <style>{`
        .otp-challenge-card {
          background: #181c24;
          border: 1px solid #2e384d;
          border-radius: 12px;
          padding: 24px;
          color: #e5e9f0;
          box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
          max-width: 440px;
          margin: 16px auto;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .otp-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 12px;
        }
        .otp-title {
          font-size: 18px;
          font-weight: 700;
          color: #f3f4f6;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .otp-badge {
          background: rgba(245, 158, 11, 0.2);
          color: #fbbf24;
          border: 1px solid #f59e0b;
          border-radius: 6px;
          padding: 2px 8px;
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
        }
        .otp-sub {
          font-size: 13px;
          color: #9ca3af;
          margin-bottom: 16px;
          line-height: 1.4;
        }
        .otp-inputs-row {
          display: flex;
          justify-content: space-between;
          gap: 8px;
          margin: 16px 0;
        }
        .otp-digit-box {
          width: 48px;
          height: 56px;
          background: #0f131a;
          border: 2px solid #374151;
          border-radius: 8px;
          color: #ffffff;
          font-size: 24px;
          font-weight: bold;
          text-align: center;
          outline: none;
          transition: border-color 0.2s, box-shadow 0.2s;
        }
        .otp-digit-box:focus {
          border-color: #3b82f6;
          box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.25);
        }
        .otp-digit-box:disabled {
          background: #1f2937;
          border-color: #374151;
          color: #6b7280;
          cursor: not-allowed;
        }
        .otp-demo-hint {
          background: #1e293b;
          border: 1px dashed #64748b;
          border-radius: 6px;
          padding: 8px 12px;
          font-size: 12px;
          color: #94a3b8;
          display: flex;
          justify-content: space-between;
          align-items: center;
          cursor: pointer;
          margin-bottom: 16px;
          transition: background 0.15s;
        }
        .otp-demo-hint:hover {
          background: #273549;
          border-color: #94a3b8;
        }
        .otp-timer-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          font-size: 12px;
          color: #9ca3af;
          margin-bottom: 16px;
        }
        .otp-timer-val {
          font-weight: 600;
          color: ${timeLeft < 15 ? '#ef4444' : '#10b981'};
        }
        .otp-error {
          background: rgba(239, 68, 68, 0.15);
          border: 1px solid #ef4444;
          color: #fca5a5;
          padding: 8px 12px;
          border-radius: 6px;
          font-size: 12px;
          margin-bottom: 16px;
          line-height: 1.3;
        }
        .otp-success {
          background: rgba(16, 185, 129, 0.15);
          border: 1px solid #10b981;
          color: #6ee7b7;
          padding: 8px 12px;
          border-radius: 6px;
          font-size: 12px;
          margin-bottom: 16px;
        }
        .otp-actions {
          display: flex;
          gap: 12px;
        }
        .otp-btn {
          flex: 1;
          padding: 10px 16px;
          border-radius: 6px;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          border: none;
          transition: opacity 0.15s, background 0.15s;
        }
        .otp-btn-primary {
          background: #2563eb;
          color: #ffffff;
        }
        .otp-btn-primary:hover:not(:disabled) {
          background: #1d4ed8;
        }
        .otp-btn-primary:disabled {
          background: #374151;
          color: #9ca3af;
          cursor: not-allowed;
        }
        .otp-btn-secondary {
          background: transparent;
          color: #9ca3af;
          border: 1px solid #374151;
        }
        .otp-btn-secondary:hover {
          background: #1f2937;
          color: #e5e7eb;
        }
      `}</style>

      <div className="otp-header">
        <div className="otp-title">
          <span>Security Step-Up</span>
        </div>
        <span className="otp-badge">OTP Required</span>
      </div>

      <p className="otp-sub">
        High-assurance verification: Enter the 6-digit security code to finalize your authentication. Single-use only.
      </p>

      {/* Demo helper pill */}
      {demoOtpHint && (
        <div
          className="otp-demo-hint"
          onClick={handleAutofillDemo}
          title="Click to automatically fill code"
        >
          <span>Demo OTP: <strong>{demoOtpHint}</strong></span>
          <span style={{ textDecoration: 'underline', color: '#60a5fa' }}>Autofill</span>
        </div>
      )}

      {/* Timer and Expiration info */}
      <div className="otp-timer-row">
        <span>Single-use Challenge ID: <code>{challengeId ? challengeId.slice(0, 8) : '...'}</code></span>
        <span>Expires in: <span className="otp-timer-val">{formatTimer(timeLeft)}</span></span>
      </div>

      {/* 6 Digit Inputs */}
      <form onSubmit={handleSubmit}>
        <div className="otp-inputs-row" onPaste={handlePaste}>
          {digits.map((digit, idx) => (
            <input
              key={idx}
              ref={(el) => (inputRefs.current[idx] = el)}
              type="text"
              inputMode="numeric"
              maxLength={1}
              value={digit}
              disabled={loading || timeLeft <= 0 || success}
              onChange={(e) => handleChange(idx, e.target.value)}
              onKeyDown={(e) => handleKeyDown(idx, e)}
              className="otp-digit-box"
              autoComplete="one-time-code"
            />
          ))}
        </div>

        {error && <div className="otp-error">{error}</div>}
        {success && <div className="otp-success">✓ Verified! ALLOW verdict cryptographically signed.</div>}

        <div className="otp-actions">
          <button
            type="button"
            className="otp-btn otp-btn-secondary"
            onClick={onCancel}
            disabled={loading}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="otp-btn otp-btn-primary"
            disabled={!isComplete || loading || timeLeft <= 0 || success}
          >
            {loading ? 'Verifying...' : 'Verify OTP'}
          </button>
        </div>
      </form>
    </div>
  );
}
