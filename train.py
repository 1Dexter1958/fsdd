import os
import pathlib
import random
import math
import argparse

from collections import OrderedDict
from typing import Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio
from torch.utils.data import DataLoader, Dataset, random_split
from torchaudio.transforms import AmplitudeToDB, MelSpectrogram
from sklearn.metrics import ConfusionMatrixDisplay, accuracy_score, confusion_matrix

# -------------------------
# Defaults (mirrors notebook)
# -------------------------
DEFAULT_AUDIO_DIR = "/kaggle/input/free-spoken-digit-dataset-fsdd/recordings/"
DEFAULT_SAVE_DIR = "./models"
# -------------------------

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Dataset(Dataset):
    def __init__(self, targ_dir: str, transform=True, max_length: int = 16) -> None:
        self.paths = list(pathlib.Path(targ_dir).glob("*.wav"))
        if len(self.paths) == 0:
            raise FileNotFoundError(f"No .wav files found in {targ_dir}")
        # labels extracted from filename like '3_jackson_45.wav'
        self.labels = [int(path.stem.split("_")[0]) for path in self.paths]
        self.transform = transform
        self.max_length = max_length

    def load_audio(self, index: int):
        audio_path = self.paths[index]
        waveform, sample_rate = torchaudio.load(audio_path, normalize=True)
        return waveform  # note: does not return sample_rate (same as notebook)

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, int]:
        waveform = self.load_audio(index)
        label = self.labels[index]

        if self.transform:
            # notebook uses sample_rate=32000 here (even though FSDD is typically 8000)
            mel_spec_transform = MelSpectrogram(sample_rate=8000, n_fft=1024, hop_length=512, n_mels=32)
            amp2db_transform = AmplitudeToDB()
            waveform = mel_spec_transform(waveform)
            waveform = amp2db_transform(waveform)

            # normalize per-sample
            waveform = (waveform - waveform.mean()) / (waveform.std() + 1e-8)

            # pad/trim to max_length on time axis (last dim)
            if waveform.shape[-1] < self.max_length:
                padding = self.max_length - waveform.shape[-1]
                waveform = torch.nn.functional.pad(waveform, (0, padding))
            elif waveform.shape[-1] > self.max_length:
                waveform = waveform[:, :, :self.max_length]

        # notebook returns waveform.squeeze(), label
        return waveform.squeeze(), label


# -------------------------
# Surrogate gradient & LIF
# -------------------------
lens = 0.5
thresh = 0.875
decay = 0.5  # decay constants (Leak)
gamma = 0.5
surroguate_type = "G"  # default in notebook

def gaussian(x, mu=0., sigma=0.5):
    return torch.exp(-((x - mu) ** 2) / (2 * sigma ** 2)) / torch.sqrt(2 * torch.tensor(math.pi)) / sigma


class AcFun_adp(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):  # input = membrane potential - threshold
        ctx.save_for_backward(input)
        return input.gt(0).float()

    @staticmethod
    def backward(ctx, grad_output):  # approximate the gradients
        input, = ctx.saved_tensors
        grad_input = grad_output.clone()

        scale = 6.0
        height = 0.15
        if surroguate_type == "G":
            temp = torch.exp(-(input ** 2) / (2 * lens ** 2)) / torch.sqrt(2 * torch.tensor(math.pi)) / lens
        elif surroguate_type == "MG":
            temp = gaussian(input, mu=0., sigma=lens) * (1.0 + height) \
                   - gaussian(input, mu=lens, sigma=scale * lens) * height \
                   - gaussian(input, mu=-lens, sigma=scale * lens) * height
        elif surroguate_type == "linear":
            temp = F.relu(1 - input.abs())
        elif surroguate_type == "slayer":
            temp = torch.exp(-5 * input.abs())
        elif surroguate_type == "sigmoid":
            temp = torch.exp(-input) / (1 + torch.exp(-input)) ** 2
        else:
            temp = torch.exp(-input.abs())
        return grad_input * temp.float() * gamma


act_fun_adp = AcFun_adp.apply


def LIF_mem_update(inputs, mem, spike):
    # mem = mem * decay * (1. - spike) + inputs
    n = float(2 ** 14)
    mem = mem * decay * (1. - spike)
    mem = torch.clip(torch.round(mem * n) / n, -14.99993896484375, 14.99993896484375)
    mem = mem + inputs
    mem = torch.clip(torch.round(mem * n) / n, -14.99993896484375, 14.99993896484375)

    temp_mem = mem - thresh
    spike = act_fun_adp(temp_mem)
    return mem, spike


# -------------------------
# Quantize / Binarize
# -------------------------
def Quantize(tensor, n_bit):
    n = float(2 ** (n_bit - 2))
    return torch.clip(torch.round(tensor * n) / n, -1.99993896484375, 1.99993896484375)


def Binarize(tensor):
    return Quantize(tensor, 16)


class BinarizeLinear(nn.Linear):
    def __init__(self, *kargs, **kwargs):
        super(BinarizeLinear, self).__init__(*kargs, **kwargs)

    def forward(self, input):
        if not hasattr(self.weight, "org"):
            self.weight.org = self.weight.data.clone()
        self.weight.data = Binarize(self.weight.org)
        out = nn.functional.linear(input, self.weight)
        if not self.bias is None:
            if not hasattr(self.bias, "org"):
                self.bias.org = self.bias.data.clone()
            out += self.bias.view(1, -1).expand_as(out)
        return out


def Get_HexData(binary_tensor):
    binary_tensor = binary_tensor.to(torch.int)

    flattened = binary_tensor.view(-1)

    if len(flattened) % 4 != 0:
        padding = torch.zeros(4 - len(flattened) % 4, dtype=torch.int)
        flattened = torch.cat((flattened, padding))

    grouped = flattened.view(-1, 4)
    grouped = torch.flip(grouped, dims=[-1])

    hex_data = []
    for group in grouped:
        binary_str = "".join([str(bit.item()) for bit in group])
        hex_value = hex(int(binary_str, 2))[2:].upper()
        hex_data.append(hex_value)

    reversed_hex_data = hex_data[::-1]
    print("倒序后的16进制数据:", reversed_hex_data)
    return reversed_hex_data


print("Load LIF Done!!!!")


# -------------------------
# GRU variants (mirrors notebook)
# -------------------------
class GRU(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, bias=False, dropout=0.0):
        super(GRU, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.dropout = dropout

        self.RZGate = nn.Linear(input_size + hidden_size, hidden_size, bias=bias)
        self.h2o = nn.Linear(hidden_size, output_size, bias=bias)
        self.relu = nn.ReLU()

        self.dropout_layer = nn.Dropout(dropout)
        self.reset_parameters()

    def reset_parameters(self):
        std = 1.0 / math.sqrt(self.hidden_size)
        for w in self.parameters():
            w.data.uniform_(-std, std)

    def forward(self, x, hidden_spike=None):
        # x: (batch, seq_length, input_size)
        if hidden_spike is None:
            hidden_spike = torch.zeros(x.size(0), self.hidden_size, device=x.device)
        else:
            hidden_spike = hidden_spike.squeeze(0)

        Encoding_mem = Encoding_spike = (torch.zeros(x.size(0), x.size(1), x.size(2))).to(device)
        Encoding_mem, Encoding_spike = LIF_mem_update(x, Encoding_mem, Encoding_spike)
        outputs = []
        for t in range(x.size(1)):
            X_spike = Encoding_spike[:, t, :]
            x_in = torch.cat((X_spike, hidden_spike), dim=1).to(device)
            hidden_spike = torch.sigmoid(self.RZGate(x_in))
            o_input = self.h2o(hidden_spike)
        return o_input


class SGRU(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, bias=False, dropout=0.0):
        super(SGRU, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.dropout = dropout

        self.relu = nn.ReLU()
        self.RZGate = nn.Linear(input_size + hidden_size, hidden_size, bias=bias)
        self.h2o = nn.Linear(hidden_size, output_size, bias=bias)

        self.dropout_layer = nn.Dropout(dropout)
        self.reset_parameters()

    def reset_parameters(self):
        std = 1.0 / math.sqrt(self.hidden_size)
        for w in self.parameters():
            w.data.uniform_(-std, std)

    def forward(self, x, hidden_spike=None):
        if hidden_spike is None:
            hidden_spike = torch.zeros(x.size(0), self.hidden_size, device=x.device)
        else:
            hidden_spike = hidden_spike.squeeze(0)

        Encoding_mem = Encoding_spike = (torch.zeros(x.size(0), x.size(1), x.size(2))).to(device)
        RZgate_mem = RZgate_spike = torch.zeros(x.size(0), self.hidden_size, device=x.device)

        Encoding_mem, Encoding_spike = LIF_mem_update(x, Encoding_mem, Encoding_spike)
        outputs = []
        for t in range(x.size(1)):
            X_spike = Encoding_spike[:, t, :]
            x_in = torch.cat((X_spike, hidden_spike), dim=1)
            RZgate_x = self.RZGate(x_in)
            RZgate_mem, RZgate_spike = LIF_mem_update(RZgate_x, RZgate_mem, RZgate_spike)
            hidden_spike = RZgate_spike
            o_input = self.h2o(hidden_spike)
        return o_input


class LSGRU(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, bias=False, dropout=0.0):
        super(LSGRU, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.dropout = dropout

        self.RZGate = BinarizeLinear(input_size + hidden_size, hidden_size, bias=bias)
        self.h2o = BinarizeLinear(hidden_size, output_size, bias=bias)

        self.dropout_layer = nn.Dropout(dropout)
        self.reset_parameters()

    def reset_parameters(self):
        std = 1.0 / math.sqrt(self.hidden_size)
        for w in self.parameters():
            w.data.uniform_(-std, std)

    def forward(self, x, hidden_spike=None):
        if hidden_spike is None:
            hidden_spike = torch.zeros(x.size(0), self.hidden_size, device=x.device)
        else:
            hidden_spike = hidden_spike.squeeze(0)

        Encoding_mem = Encoding_spike = (torch.zeros(x.size(0), x.size(1), x.size(2))).to(device)
        RZgate_mem = RZgate_spike = torch.zeros(x.size(0), self.hidden_size, device=x.device)

        Encoding_mem, Encoding_spike = LIF_mem_update(x, Encoding_mem, Encoding_spike)
        outputs = []
        for t in range(x.size(1)):
            X_spike = Encoding_spike[:, t, :]
            x_in = torch.cat((X_spike, hidden_spike), dim=1)
            RZgate_x = self.RZGate(x_in)
            RZgate_mem, RZgate_spike = LIF_mem_update(RZgate_x, RZgate_mem, RZgate_spike)
            hidden_spike = RZgate_spike
            o_input = self.h2o(hidden_spike)
        return o_input


class OLSGRU(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, bias=False):
        super(OLSGRU, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.RZGate = BinarizeLinear(input_size + hidden_size, hidden_size, bias=bias)
        self.h2o = BinarizeLinear(hidden_size, output_size, bias=bias)

    def forward(self, x, hidden_spike=None):
        if hidden_spike is None:
            hidden_spike = torch.zeros(x.size(0), self.hidden_size, device=x.device)
        else:
            hidden_spike = hidden_spike.squeeze(0)

        Encoding_mem = Encoding_spike = (torch.zeros(x.size(0), x.size(1), x.size(2))).to(device)
        RZgate_mem = RZgate_spike = torch.zeros(x.size(0), self.hidden_size, device=x.device)

        Encoding_mem, Encoding_spike = LIF_mem_update(x, Encoding_mem, Encoding_spike)

        o_input = Encoding_spike
        return o_input


# -------------------------
# PositionalEncoding + SpeechTransformer
# -------------------------
class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=5000):
        super(PositionalEncoding, self).__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1).float()
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * -(math.log(10000.0) / d_model))

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer("pe", pe)

    def forward(self, x):
        # x shape: (batch_size, time_steps, d_model)
        return x + self.pe[:, :x.size(1)]


class SpeechTransformer(nn.Module):
    def __init__(self, num_classes=10, d_model=32, nhead=4, num_layers=4):
        super(SpeechTransformer, self).__init__()
        self.positional_encoding = PositionalEncoding(d_model)

        # classifier choices from notebook; default is OLSGRU in the notebook
        self.classifier = OLSGRU(d_model, 64, 10)

    def forward(self, x):
        # x shape: (batch_size, mel_bins, time_steps)
        x = x.transpose(1, 2)  # (batch_size, time_steps, mel_bins)
        x = self.positional_encoding(x)
        x = self.classifier(x)
        return x


# -------------------------
# Training & feature export (follows notebook flow)
# -------------------------
def run_train_and_export(audio_dir: str, save_dir: str, epochs: int = 5):
    os.makedirs(save_dir, exist_ok=True)
    dataset = Dataset(audio_dir, transform=True)

    train_size = int(0.9 * len(dataset))
    test_size = len(dataset) - train_size
    train_dataset, test_dataset = random_split(dataset, [train_size, test_size])

    train_loader = DataLoader(train_dataset, batch_size=100, shuffle=True)
    test_loader = DataLoader(test_dataset, batch_size=100)

    model = SpeechTransformer().to(device)

    # If an srnn_best_model.pth exists at the kaggle path in the notebook, load it (keeps same behavior)
    srnn_path = "/kaggle/working/models/srnn_best_model.pth"
    if os.path.exists(srnn_path):
        try:
            model.load_state_dict(torch.load(srnn_path))
            print(f"Loaded pretrained {srnn_path}")
        except Exception as e:
            print("Could not load srnn_best_model.pth:", e)

    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=80, gamma=0.1)

    best_val_accuracy = 0.0

    for epoch in range(epochs):
        model.train()
        train_loss = 0.0
        for inputs, labels in train_loader:
            inputs, labels = inputs.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(inputs)
            # In notebook classifier returns (batch, time, dim) for OLSGRU; to compute loss we need logits
            # In original notebook they later treat outputs as logits; to be faithful we try to reduce to logits:
            if outputs.ndim == 3:
                # aggregate over time (mean) to produce (batch, d_model) then map to 10 classes via a linear if needed
                outputs = outputs.mean(dim=1)
                # map to 10 classes if dims mismatch
                if outputs.size(1) != 10:
                    # create a temporary linear mapper on-the-fly
                    mapper = nn.Linear(outputs.size(1), 10).to(outputs.device)
                    outputs = mapper(outputs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()

        # Validation
        model.eval()
        val_loss = 0.0
        correct = 0
        total = 0
        with torch.no_grad():
            for inputs, labels in test_loader:
                inputs, labels = inputs.to(device), labels.to(device)
                outputs = model(inputs)
                if outputs.ndim == 3:
                    outputs = outputs.mean(dim=1)
                    if outputs.size(1) != 10:
                        mapper = nn.Linear(outputs.size(1), 10).to(outputs.device)
                        outputs = mapper(outputs)
                loss = criterion(outputs, labels)
                val_loss += loss.item()
                _, predicted = torch.max(outputs, 1)
                correct += (predicted == labels).sum().item()
                total += labels.size(0)

        train_loss_avg = train_loss / len(train_loader)
        val_loss_avg = val_loss / len(test_loader)
        val_accuracy = 100 * correct / total
        print(f"Epoch {epoch+1}, Train Loss: {train_loss_avg:.4f}, Val Loss: {val_loss_avg:.4f}, Val Accuracy: {val_accuracy:.2f}%")

        if val_accuracy > best_val_accuracy:
            best_val_accuracy = val_accuracy
            save_path = os.path.join(save_dir, "lsrnn_best_model.pth")
            torch.save(model.state_dict(), save_path)
            print(f"Best model saved with Val Accuracy: {val_accuracy:.2f}% at {save_path}")

    # -------------------------
    # Feature extraction (like notebook)
    # -------------------------
    # Load the saved best model (notebook loads /kaggle/working/models/lsrnn_best_model.pth)
    saved_path = os.path.join(save_dir, "lsrnn_best_model.pth")
    if os.path.exists(saved_path):
        try:
            model.load_state_dict(torch.load(saved_path, map_location=device))
            print(f"Loaded best model from {saved_path}")
        except Exception as e:
            print("Could not load saved best model:", e)

    # prepare to extract features using Tran_Model = SpeechTransformer()
    Tran_Model = SpeechTransformer().to(device)
    # try to load saved trained weights into Tran_Model if available
    if os.path.exists(saved_path):
        try:
            Tran_Model.load_state_dict(torch.load(saved_path, map_location=device))
        except Exception:
            pass

    all_features = []
    all_labels = []

    model.eval()
    with torch.no_grad():
        for inputs, labels in test_loader:
            inputs, labels = inputs.to(device), labels.to(device)
            feature = Tran_Model(inputs)
            # if 3D, aggregate to (batch, dim)
            if feature.ndim == 3:
                feature = feature.mean(dim=1)
            feature_np = feature.cpu().detach().numpy()
            labels_np = labels.cpu().detach().numpy()
            all_features.append(feature_np)
            all_labels.append(labels_np)

    all_features = np.concatenate(all_features, axis=0)
    all_labels = np.concatenate(all_labels, axis=0)
    print(all_features.shape)
    print(all_labels.shape)
    np.save(os.path.join(save_dir, "FSDD_test_data.npy"), all_features)
    np.save(os.path.join(save_dir, "FSDD_test_label.npy"), all_labels)
    print("Saved test features and labels to", save_dir)


# -------------------------
# CLI
# -------------------------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--data_dir", type=str, default=DEFAULT_AUDIO_DIR, help="Path to recordings/ (wav files)")
    p.add_argument("--save_dir", type=str, default=DEFAULT_SAVE_DIR, help="Directory to save models/features")
    p.add_argument("--epochs", type=int, default=5)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print("Device:", device)
    print("Using data dir:", args.data_dir)
    run_train_and_export(args.data_dir, args.save_dir, epochs=args.epochs)