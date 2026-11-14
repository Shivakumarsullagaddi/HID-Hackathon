import React, { useState, useRef, useEffect } from 'react';
import OtpChallengeBox from './components/OtpChallengeBox';

export default function App() {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [cameraActive, setCameraActive] = useState(false);
  const [error, setError] = useState(null);
  const [showEnroll, setShowEnroll] = useState(false);
  const [enrolName, setEnrolName] = useState('');
  const [enrolEmail, setEnrolEmail] = useState('');
  const [enrolLoading, setEnrolLoading] = useState(false);
  const [enrolMsg, setEnrolMsg] = useState(null);
  const videoRef = useRef(null);

  // Initialize webcam
  useEffect(() => {
    let stream = null;
    navigator.mediaDevices?.getUserMedia({ video: { width: 640, height: 480 } })
      .then((s) => {
        stream = s;
        if (videoRef.current) {
          videoRef.current.srcObject = s;
          videoRef.current.play();
          setCameraActive(true);
        }
      })
      .catch((err) => {
        console.warn('Camera access error:', err);
      });

    return () => {
      if (stream) {
        stream.getTracks().forEach((track) => track.stop());
      }
    };
  }, []);

  // Capture frames from video
  const captureBurst = (count = 5) => {
    const video = videoRef.current;
    if (!video) return [];
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    const ctx = canvas.getContext('2d');
    const frames = [];

    for (let i = 0; i < count; i++) {
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      frames.push(canvas.toDataURL('image/jpeg', 0.9));
    }
    return frames;
  };

  // Perform face verification
  const handleVerify = async () => {
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const frames = captureBurst(5);
      const res = await fetch('/api/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ frames }),
      });
      const data = await res.json();
      setResult(data);
    } catch (err) {
      setError(err.message || 'Verification request failed');
    } finally {
      setLoading(false);
    }
  };

  // Handle enrollment with email
  const handleEnroll = async (e) => {
    e.preventDefault();
    if (!enrolName.trim()) return;
    setEnrolLoading(true);
    setEnrolMsg(null);
    try {
      const frames = captureBurst(5);
      const res = await fetch('/api/enrol', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: enrolName.trim(),
          email: enrolEmail.trim() || null,
          frames,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.reason || 'Enrollment failed');
      setEnrolMsg(`Successfully enrolled ${enrolName} (${data.user_id})!`);
      setEnrolName('');
      setEnrolEmail('');
    } catch (err) {
      setEnrolMsg(`Error: ${err.message}`);
    } finally {
      setEnrolLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 24, fontFamily: 'sans-serif', color: '#e2e8f0' }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, margin: 0 }}>Spot-a-Spoof Kiosk</h1>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button
            onClick={() => setShowEnroll(!showEnroll)}
            style={{
              background: '#0284c7',
              color: '#fff',
              border: 'none',
              padding: '6px 14px',
              borderRadius: 6,
              fontSize: 13,
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            {showEnroll ? 'Close Enrollment' : '+ Enroll User'}
          </button>
          <span style={{ fontSize: 13, background: '#1e293b', padding: '4px 10px', borderRadius: 4 }}>
            Security Hardened
          </span>
        </div>
      </header>

      {/* Enrollment Modal / Drawer */}
      {showEnroll && (
        <div style={{ background: '#1e293b', border: '1px solid #38bdf8', padding: 16, borderRadius: 8, marginBottom: 20 }}>
          <h3 style={{ margin: '0 0 12px 0', fontSize: 16, color: '#38bdf8' }}>Register New User (Face + Email)</h3>
          <form onSubmit={handleEnroll} style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <input
              type="text"
              placeholder="Full Name"
              value={enrolName}
              onChange={(e) => setEnrolName(e.target.value)}
              required
              style={{ flex: 1, minWidth: 150, padding: 8, borderRadius: 6, background: '#0f172a', border: '1px solid #475569', color: '#fff' }}
            />
            <input
              type="email"
              placeholder="Registered Email (for OTP delivery)"
              value={enrolEmail}
              onChange={(e) => setEnrolEmail(e.target.value)}
              style={{ flex: 1, minWidth: 200, padding: 8, borderRadius: 6, background: '#0f172a', border: '1px solid #475569', color: '#fff' }}
            />
            <button
              type="submit"
              disabled={enrolLoading || !cameraActive}
              style={{ padding: '8px 16px', background: '#10b981', color: '#fff', border: 'none', borderRadius: 6, fontWeight: 600, cursor: 'pointer' }}
            >
              {enrolLoading ? 'Enrolling…' : 'Capture & Enroll'}
            </button>
          </form>
          {enrolMsg && <p style={{ margin: '10px 0 0 0', fontSize: 13, color: enrolMsg.startsWith('Error') ? '#f87171' : '#6ee7b7' }}>{enrolMsg}</p>}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        {/* Camera feed */}
        <div style={{ background: '#0f172a', padding: 16, borderRadius: 12, textAlign: 'center' }}>
          <div style={{ position: 'relative', width: '100%', height: 320, background: '#000', borderRadius: 8, overflow: 'hidden' }}>
            <video ref={videoRef} playsInline muted style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          </div>
          <button
            onClick={handleVerify}
            disabled={loading || !cameraActive}
            style={{
              marginTop: 16,
              width: '100%',
              padding: '12px 20px',
              background: '#2563eb',
              color: '#fff',
              border: 'none',
              borderRadius: 8,
              fontSize: 16,
              fontWeight: 600,
              cursor: loading ? 'not-allowed' : 'pointer',
            }}
          >
            {loading ? 'Analyzing...' : 'Verify Identity'}
          </button>
        </div>

        {/* Results & Step-Up Card */}
        <div style={{ background: '#0f172a', padding: 20, borderRadius: 12 }}>
          <h2>Status</h2>
          {error && <div style={{ color: '#ef4444', marginBottom: 12 }}>{error}</div>}

          {/* Normal ALLOW / DENY states */}
          {result && result.decision === 'ALLOW' && (
            <div style={{ background: '#064e3b', border: '1px solid #10b981', padding: 16, borderRadius: 8 }}>
              <h3 style={{ color: '#6ee7b7', margin: '0 0 8px 0' }}>✓ ALLOW: Identity Verified</h3>
              <p style={{ margin: 0, fontSize: 14 }}>{result.reason}</p>
              {result.signature && (
                <p style={{ fontSize: 11, color: '#a7f3d0', wordBreak: 'break-all', marginTop: 8 }}>
                  Signature: {result.signature}
                </p>
              )}
            </div>
          )}

          {result && result.decision === 'DENY' && (
            <div style={{ background: '#450a0a', border: '1px solid #ef4444', padding: 16, borderRadius: 8 }}>
              <h3 style={{ color: '#fca5a5', margin: '0 0 8px 0' }}>✗ DENY: Access Rejected</h3>
              <p style={{ margin: 0, fontSize: 14 }}>{result.reason}</p>
            </div>
          )}

          {/* REQUEST_VERIFICATION state: renders OtpChallengeBox above Blink check */}
          {result?.decision === 'REQUEST_VERIFICATION' && (
            <div>
              {result.challenge_id && (
                <OtpChallengeBox
                  challengeId={result.challenge_id}
                  demoOtpHint={result.demo_otp}
                  maskedEmail={result.masked_email}
                  onVerified={(verdict) => setResult(verdict)}
                  onCancel={() => setResult(null)}
                />
              )}
              <div style={{ marginTop: 14, background: '#1e293b', padding: 14, borderRadius: 8, border: '1px solid #334155' }}>
                <h4 style={{ margin: '0 0 6px 0', fontSize: 13, color: '#f1f5f9' }}>Alternative: Extra check: blink</h4>
                <p style={{ margin: '0 0 10px 0', fontSize: 12, color: '#94a3b8' }}>
                  If you cannot access your registered email, blink twice slowly while looking at the camera.
                </p>
              </div>
            </div>
          )}

          {!result && !loading && (
            <p style={{ color: '#94a3b8', fontSize: 14 }}>Align your face in the camera and click "Verify Identity".</p>
          )}
        </div>
      </div>
    </div>
  );
}
