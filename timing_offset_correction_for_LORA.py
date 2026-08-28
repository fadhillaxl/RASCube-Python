import numpy as np

def generate_lora_chirp(sf: int, bw: float, fs: float, is_upchirp: bool = True) -> np.ndarray:
    """Generates an unmodulated baseband LoRa upchirp or downchirp."""
    N = 2**sf
    T = N / bw
    num_samples = int(fs * T)
    t = np.linspace(0, T, num_samples, endpoint=False)
    
    # Chirp rate mu = BW / T
    mu = bw / T
    if not is_upchirp:
        mu = -mu
        
    # Phase = 2 * pi * (f0 * t + 0.5 * mu * t^2)
    phase = 2 * np.pi * (-bw / 2 * t + 0.5 * mu * t**2)
    return np.exp(1j * phase)

def estimate_and_correct_lora_cfo(
    rx_signal: np.ndarray, 
    sf: int, 
    bw: float, 
    fs: float, 
    upchirp_start: int, 
    downchirp_start: int
) -> tuple[np.ndarray, float]:
    """
    Estimates CFO by combining de-chirped upchirp and downchirp peaks,
    then applies dynamic complex exponential rotation across the signal.
    """
    N = 2**sf
    T = N / bw
    samples_per_symbol = int(fs * T)

    # 1. Generate ideal reference chirps
    ref_downchirp = generate_lora_chirp(sf, bw, fs, is_upchirp=False)
    ref_upchirp = generate_lora_chirp(sf, bw, fs, is_upchirp=True)

    # 2. Extract preamble segments
    rx_up = rx_signal[upchirp_start : upchirp_start + samples_per_symbol]
    rx_down = rx_signal[downchirp_start : downchirp_start + samples_per_symbol]

    # 3. Multiply by reference to de-chirp (Converts linear chirp into a single tone)
    dechirped_up = rx_up * ref_downchirp
    dechirped_down = rx_down * ref_upchirp

    # 4. Compute FFTs to find peak frequency indices
    fft_up = np.abs(np.fft.fft(dechirped_up, n=samples_per_symbol))
    fft_down = np.abs(np.fft.fft(dechirped_down, n=samples_per_symbol))

    k_up = np.argmax(fft_up)
    k_down = np.argmax(fft_down)

    # Wrap indices around FFT center
    if k_up > samples_per_symbol / 2:
        k_up -= samples_per_symbol
    if k_down > samples_per_symbol / 2:
        k_down -= samples_per_symbol

    # 5. Extract CFO bin delta and convert to Hertz
    # CFO = (k_up - k_down) / 2 bins
    cfo_bins = (k_up - k_down) / 2.0
    cfo_hz = cfo_bins * (fs / samples_per_symbol)

    # 6. Apply CFO correction multiplier across full signal
    t = np.arange(len(rx_signal)) / fs
    correction_vector = np.exp(-1j * 2 * np.pi * cfo_hz * t)
    corrected_signal = rx_signal * correction_vector

    return corrected_signal, cfo_hz

# --- Simulation & Test ---
if __name__ == "__main__":
    SF = 7         # Spreading Factor
    BW = 125e3     # Bandwidth 125 kHz
    FS = 1e6       # Sampling Rate 1 MHz
    
    # Generate reference preamble: 8 Upchirps + 2 Downchirps
    upchirp = generate_lora_chirp(SF, BW, FS, is_upchirp=True)
    downchirp = generate_lora_chirp(SF, BW, FS, is_upchirp=False)
    
    preamble = np.concatenate([np.tile(upchirp, 8), np.tile(downchirp, 2)])
    
    # Inject 4.2 kHz Carrier Frequency Offset
    true_cfo = 4200.0  # Hz
    t_sim = np.arange(len(preamble)) / FS
    rx_distorted = preamble * np.exp(1j * 2 * np.pi * true_cfo * t_sim)

    # Calculate symbol indices
    sym_len = int(FS * (2**SF / BW))
    idx_up = 0              # 1st upchirp
    idx_down = 8 * sym_len  # 1st downchirp

    # Correct offset
    rx_corrected, estimated_cfo = estimate_and_correct_lora_cfo(
        rx_distorted, SF, BW, FS, idx_up, idx_down
    )

    print(f"Injected CFO:  {true_cfo:.2f} Hz")
    print(f"Estimated CFO: {estimated_cfo:.2f} Hz")