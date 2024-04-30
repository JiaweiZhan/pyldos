'''
Note: \chi_0 = pdepg.T @ np.diag(pdepeig) @ pdepg.conj(), where pdepg is function with shape [npdep, nmill]
'''
import json
import os
import numpy as np
from scipy.linalg import eigh
import math
from typing import List
from loguru import logger
import torch
import time

from westpy import qe_io

from qcat.io_kernel import pyscfHelper, QEProvider
from qcat.utils import setLogger
from .core import *

setLogger()

class PDEP2AO(object):
    def __init__(self,
                 wfc_fname,
                 wstat_folder=None,
                 basis: str = "cc-pvqz",
                 unit: str = "B",
                 exp_to_discard = 0.1,
                 ):
        assert os.path.exists(wfc_fname), f"{wfc_fname} does not exist"
        if wstat_folder is not None:
            assert os.path.exists(wstat_folder), f"{wstat_folder} does not exist"

        self.qe = qe_io(wfc_fname, wstat_folder)
        dft_provider = QEProvider(wfc_fname)
        self.pyscf_obj = pyscfHelper(dft_provider, basis, unit, exp_to_discard)
        b = np.array([self.qe.b1, self.qe.b2, self.qe.b3])
        g_vec = self.qe.mill @ b      # [nmill, 3]
        g_vec_norm = np.linalg.norm(g_vec, axis=-1)
        self.gd4pi = g_vec_norm[None, :] / math.sqrt(4 * math.pi) # [1, nmill]


    def getChiSpecDecomp(self):
        eigval = self.qe.pdepeig / (1 - self.qe.pdepeig)
        return eigval, self.qe.pdepg * self.gd4pi # [npdep, nmill]

    @staticmethod
    def decom2Eigen(spec_val: np.ndarray, # [npdep]
                    spec_vec: np.ndarray, # [npdep, nmill],
                    tol: float=1e-10,
                    ):
        chi_eigval, chi_eigvec = oeigh(spec_vec.T, spec_val, tol=tol) # [nmill, npdep]
        return chi_eigval, chi_eigvec.T # [npdep, nmill]


    def getAO_G(self,
                shls_slice=None,
                cutoff=None,
                use_lcao: bool=False,
                lcao_fname=None,
                remove_shls: List=['s', 'g', 'h', 'i', 'j'],
                eval_overlap_r: bool=False,
                ):
        basis_cpu = self.pyscf_obj.get_basis(shls_slice, cutoff, use_lcao, lcao_fname) # (nAO, nx, ny, nz)
        basis_cpu, labels, mask = clear_basis(basis_cpu, self.pyscf_obj.spheric_labels, remove_shls)
        nbasis = basis_cpu.shape[0]
        logger.info(f"nbasis: {nbasis}")

        ovm = None
        if eval_overlap_r:
            ovm = np.tensordot(basis_cpu, basis_cpu, axes=([1, 2, 3], [1, 2, 3]))
            norm = np.sqrt(np.diag(ovm))
            norm_matrix = norm[:, None] @ norm[None, :]
            ovm /= norm_matrix

        basis_cpu = torch.fft.fftn(torch.as_tensor(basis_cpu, dtype=torch.float32), dim=(1, 2, 3), norm='forward').numpy()
        basis_g = np.zeros((nbasis, len(self.qe.mill)), dtype=basis_cpu.dtype) # [nbasis, nmill]
        mill_x, mill_y, mill_z = self.qe.mill.T
        basis_g[:] = basis_cpu[:, mill_x, mill_y, mill_z]
        norm_basis_g = np.sqrt(np.diag((basis_g @ basis_g.T.conj()).real) * 2)
        basis_g /= norm_basis_g[:, None] # [nbasis, nmill]
        return basis_g, labels, mask, ovm

    def compute_S(self,
                  basis_g: np.ndarray, # [nbasis, nmill]
                  pyscf_overlap: bool=False,
                  mask=None,
                  ):
        if not pyscf_overlap:
            return (basis_g.conj() @ basis_g.T).real * 2
        else:
            ovm = self.pyscf_obj.cell.pbc_intor('int1e_ovlp_sph')
            return ovm[mask, :][:, mask]

    @staticmethod
    def compute_QAQ(basis_g: np.ndarray, # [nbasis, nmill]
                    eigvec: np.ndarray,  # [npdep, nmill]
                    eigval: np.ndarray,  # [npdep]
                    ):
        twoOrbitalMat = basis_g.conj() @ eigvec.T
        return (twoOrbitalMat.real * 2) @ np.diag(eigval) @ ((twoOrbitalMat.T.conj()).real * 2)


    def compute_pdep(self,
                     s: np.ndarray,
                     qaq : np.ndarray,
                     basis_g: np.ndarray,
                     tol: float=1e-1,
                     npdep=None,
                     ):
        chi_eigval_fit, coeff = eigh(qaq, s)
        chi_eigvec_fit = coeff.T @ basis_g # [nbasis, nmill]
        chibar_decom_eigval_fit = chi_eigval_fit
        chibar_decom_eigvec_fit = np.divide(chi_eigvec_fit,
                                            self.gd4pi,
                                            out=np.zeros_like(chi_eigvec_fit),
                                            where=self.gd4pi!=0) # [nbasis, nmill]
        logger.info("Compute Eigen for chibar...")
        chibar_eigval_fit, chibar_eigvec_fit = self.decom2Eigen(chibar_decom_eigval_fit,
                                                                chibar_decom_eigvec_fit,
                                                                tol=tol,
                                                                ) # [nbasis], [nbasis, nmill]
        chi0bar_eigval_fit = chibar_eigval_fit / (1 + chibar_eigval_fit)

        # chibar and chi0bar share same eigenvectors
        if npdep is None:
            npdep = chi0bar_eigval_fit.size
        chi0bar_eigval_fit = chi0bar_eigval_fit[:npdep]
        chibar_eigvec_fit = chibar_eigvec_fit[:npdep]
        return chi0bar_eigval_fit, chibar_eigvec_fit


    def run(self,
            outDir: str='./log',
            pyscf_overlap: bool=False,
            qaq_threshold=None,
            precision: str='float',
            tol: float=1e-1,
            prefix: str='westpy',
            save_pdep: bool = True,
            **kwargs,
            ):
        start_time = time.time()

        possible_precision = ['float', 'double']
        assert precision in possible_precision, f"precision should be one of {possible_precision}"
        if not os.path.exists(outDir):
            os.makedirs(outDir)
        chi_decom_eigval, chi_decom_eigvec = self.getChiSpecDecomp() # [npdep], [npdep, nmill]
        npdep = chi_decom_eigval.size
        basis_g, labels, mask, S = self.getAO_G(**kwargs) # [nbasis, nmill]

        if precision == 'float':
            basis_g = basis_g.astype(np.complex64)
            chi_decom_eigvec = chi_decom_eigvec.astype(np.complex64)
            chi_decom_eigval = chi_decom_eigval.astype(np.float32)
            if S is not None:
                S = S.astype(np.float32)

        if S is None:
            S = self.compute_S(basis_g, pyscf_overlap, mask) # [nbasis, nbasis]

        QAQ = self.compute_QAQ(basis_g, chi_decom_eigvec, chi_decom_eigval) # [nbasis, nbasis]

        if qaq_threshold:
            logger.info(f"Apply threshold {qaq_threshold:^5.2e} to QAQ matrix.")
            qaq_threshold = np.abs(qaq_threshold)
            QAQ = np.where(np.abs(QAQ) < qaq_threshold, 0.0, QAQ)
        sparse_ratio = (QAQ==0.0).sum() / QAQ.size
        logger.info(f"QAQ matrix sparse ratio: {sparse_ratio*100:^5.2f}%.")

        label_fname = os.path.join(outDir, 'orbital_labels.json')
        with open(label_fname, 'w') as f:
            json.dump(labels.tolist(), f, indent=4)
        logger.info(f"Orbital labels are saved in {label_fname}")

        s_fname = os.path.join(outDir, 'S.npy')
        np.save(s_fname, S)
        logger.info(f"S matrix is saved in {s_fname}")

        qaq_fname = os.path.join(outDir, 'QAQ.npy')
        np.save(qaq_fname, QAQ)
        logger.info(f"QAQ matrix is saved in {qaq_fname}")

        pdep_eigval_fit, pdep_eigvec_fit = self.compute_pdep(S, QAQ, basis_g, tol, npdep)

        if save_pdep:
            prefix = os.path.join(outDir, prefix)
            self.qe.write_wstat(pdep_eigval_fit, pdep_eigvec_fit, prefix=prefix, eig_mat='chi_0')
        logger.info(f"Running Time: {time.time() - start_time:^8.2f}s")
        return pdep_eigval_fit, pdep_eigvec_fit

def tcddrf2PDEP(wfc_name: str,
                qaq: np.ndarray,
                s: np.ndarray,
                npdep: int,
                basis: str = "cc-pvqz",
                unit: str = "B",
                exp_to_discard = 0.1,
                tol: float=1e-1,
                outDir: str='./log_tcddrf2PDEP',
                prefix: str='tcddrf2PDEP',
                **kwargs):
    start_time = time.time()
    pdep2ao = PDEP2AO(wfc_name, basis=basis, unit=unit, exp_to_discard=exp_to_discard)
    basis_g, _, _, _ = pdep2ao.getAO_G(**kwargs)
    
    pdep_eigval_fit, pdep_eigvec_fit = pdep2ao.compute_pdep(s, qaq, basis_g, tol, npdep)
    if not os.path.exists(outDir):
        os.makedirs(outDir)
    prefix = os.path.join(outDir, prefix)
    pdep2ao.qe.write_wstat(pdep_eigval_fit, pdep_eigvec_fit, prefix=prefix, eig_mat='chi_0')
    logger.info(f"Running Time: {time.time() - start_time:^8.2f}s")
    return pdep_eigval_fit, pdep_eigvec_fit
