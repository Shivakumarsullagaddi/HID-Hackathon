import React, { useState, useRef, useEffect } from 'react';
import OtpChallengeBox from './components/OtpChallengeBox';

export default function App() {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [cameraActive, setCameraActive] = useState(false);
  const [error, setError] = useState(null);
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

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 24, fontFamily: 'sans-serif', color: '#e2e8f0' }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, margin: 0 }}>Spot-a-Spoof Kiosk</h1>
        <span style={{ fontSize: 13, background: '#1e293b', padding: '4px 10px', borderRadius: 4 }}>
          Security Hardened
        </span>
      </header>

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

          {/* Minimal 5-line integration for REQUEST_VERIFICATION state */}
          {result?.decision === 'REQUEST_VERIFICATION' && result?.challenge_id && (
            <OtpChallengeBox
              challengeId={result.challenge_id}
              demoOtpHint={result.demo_otp}
              onVerified={(verdict) => setResult(verdict)}
              onCancel={() => setResult(null)}
            />
          )}

          {!result && !loading && (
            <p style={{ color: '#94a3b8', fontSize: 14 }}>Align your face in the camera and click "Verify Identity".</p>
          )}
        </div>
      </div>
    </div>
  );
}
