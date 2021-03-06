import numpy as np
import sys
import gc
from pycuda import gpuarray as ga
import pycuda.driver as cuda

from chroma.tools import profile_if_possible
from chroma import event
from chroma.gpu.tools import get_cu_module, cuda_options, GPUFuncs, \
    chunk_iterator, to_float3


class GPUPhotons(object):
    def __init__(self, photons, ncopies=1):
        """Load ``photons`` onto the GPU, replicating as requested.

           Args:
               - photons: chroma.Event.Photons
                   Photon state information to load onto GPU
               - ncopies: int, *optional*
                   Number of times to replicate the photons
                   on the GPU.  This is used if you want
                   to propagate the same event many times,
                   for example in a likelihood calculation.

                   The amount of GPU storage will be proportionally
                   larger if ncopies > 1, so be careful.
        """
        nphotons = len(photons)
        self.pos = ga.empty(shape=nphotons*ncopies, dtype=ga.vec.float3)
        self.dir = ga.empty(shape=nphotons*ncopies, dtype=ga.vec.float3)
        self.pol = ga.empty(shape=nphotons*ncopies, dtype=ga.vec.float3)
        self.wavelengths = ga.empty(shape=nphotons*ncopies, dtype=np.float32)
        self.t = ga.empty(shape=nphotons*ncopies, dtype=np.float32)
        self.last_hit_triangles = ga.empty(shape=nphotons*ncopies, dtype=np.int32)
        self.flags = ga.empty(shape=nphotons*ncopies, dtype=np.uint32)
        self.weights = ga.empty(shape=nphotons*ncopies, dtype=np.float32)

        # Assign the provided photons to the beginning (possibly
        # the entire array if ncopies is 1
        self.pos[:nphotons].set(to_float3(photons.pos))
        self.dir[:nphotons].set(to_float3(photons.dir))
        self.pol[:nphotons].set(to_float3(photons.pol))
        self.wavelengths[:nphotons].set(photons.wavelengths.astype(np.float32))
        self.t[:nphotons].set(photons.t.astype(np.float32))
        self.last_hit_triangles[:nphotons].set(photons.last_hit_triangles.astype(np.int32))
        self.flags[:nphotons].set(photons.flags.astype(np.uint32))
        self.weights[:nphotons].set(photons.weights.astype(np.float32))

        module = get_cu_module('propagate.cu', options=cuda_options)
        self.gpu_funcs = GPUFuncs(module)

        # Replicate the photons to the rest of the slots if needed
        if ncopies > 1:
            max_blocks = 1024
            nthreads_per_block = 64
            for first_photon, photons_this_round, blocks in \
                    chunk_iterator(nphotons, nthreads_per_block, max_blocks):
                self.gpu_funcs.photon_duplicate(np.int32(first_photon), np.int32(photons_this_round),
                                                self.pos, self.dir, self.wavelengths, self.pol, self.t, 
                                                self.flags, self.last_hit_triangles, self.weights,
                                                np.int32(ncopies-1), 
                                                np.int32(nphotons),
                                                block=(nthreads_per_block,1,1), grid=(blocks, 1))


        # Save the duplication information for the iterate_copies() method
        self.true_nphotons = nphotons
        self.ncopies = ncopies

    def get(self):
        pos = self.pos.get().view(np.float32).reshape((len(self.pos),3))
        dir = self.dir.get().view(np.float32).reshape((len(self.dir),3))
        pol = self.pol.get().view(np.float32).reshape((len(self.pol),3))
        wavelengths = self.wavelengths.get()
        t = self.t.get()
        last_hit_triangles = self.last_hit_triangles.get()
        flags = self.flags.get()
        weights = self.weights.get()
        return event.Photons(pos, dir, pol, wavelengths, t, last_hit_triangles, flags, weights)
        
    def get_hits(self, gpu_detector, target_flag=(0x1<<2), nthreads_per_block=64, max_blocks=1024,
               start_photon=None, nphotons=None):
        '''Return a map of GPUPhoton objects containing only photons that
        have a particular bit set in their history word and were detected by
        a channel.'''
        cuda.Context.get_current().synchronize()
        index_counter_gpu = ga.zeros(shape=1, dtype=np.uint32)
        cuda.Context.get_current().synchronize()
        if start_photon is None:
            start_photon = 0
        if nphotons is None:
            nphotons = self.pos.size - start_photon

        # First count how much space we need
        for first_photon, photons_this_round, blocks in chunk_iterator(nphotons, nthreads_per_block, max_blocks):
            self.gpu_funcs.count_photon_hits(np.int32(start_photon+first_photon), 
                                         np.int32(photons_this_round),
                                         np.uint32(target_flag),
                                         self.flags,
                                         gpu_detector.solid_id_map,
                                         self.last_hit_triangles,
                                         gpu_detector.detector_gpu,
                                         index_counter_gpu,
                                         block=(nthreads_per_block,1,1), 
                                         grid=(blocks, 1))
        cuda.Context.get_current().synchronize()
        reduced_nphotons = int(index_counter_gpu.get()[0])
        
        # Then allocate new storage space
        pos = ga.empty(shape=reduced_nphotons, dtype=ga.vec.float3)
        dir = ga.empty(shape=reduced_nphotons, dtype=ga.vec.float3)
        pol = ga.empty(shape=reduced_nphotons, dtype=ga.vec.float3)
        wavelengths = ga.empty(shape=reduced_nphotons, dtype=np.float32)
        t = ga.empty(shape=reduced_nphotons, dtype=np.float32)
        last_hit_triangles = ga.empty(shape=reduced_nphotons, dtype=np.int32)
        flags = ga.empty(shape=reduced_nphotons, dtype=np.uint32)
        weights = ga.empty(shape=reduced_nphotons, dtype=np.float32)
        channels = ga.empty(shape=reduced_nphotons, dtype=np.int32)

        # And finaly copy hits, if there are any
        if reduced_nphotons > 0:
            index_counter_gpu.fill(0)
            for first_photon, photons_this_round, blocks in \
                    chunk_iterator(nphotons, nthreads_per_block, max_blocks):
                self.gpu_funcs.copy_photon_hits(np.int32(start_photon+first_photon), 
                                            np.int32(photons_this_round), 
                                            np.uint32(target_flag),
                                            gpu_detector.solid_id_map,
                                            gpu_detector.detector_gpu,
                                            index_counter_gpu, 
                                            self.pos, self.dir, self.wavelengths, self.pol, self.t, self.flags, self.last_hit_triangles, self.weights,
                                            pos, dir, wavelengths, pol, t, flags, last_hit_triangles, weights, channels,
                                            block=(nthreads_per_block,1,1), 
                                            grid=(blocks, 1))
            assert index_counter_gpu.get()[0] == reduced_nphotons
            
        pos = pos.get().view(np.float32).reshape((len(pos),3))
        dir = dir.get().view(np.float32).reshape((len(dir),3))
        pol = pol.get().view(np.float32).reshape((len(pol),3))
        wavelengths = wavelengths.get()
        t = t.get()
        last_hit_triangles = last_hit_triangles.get()
        flags = flags.get()
        weights = weights.get()
        channels = channels.get()
        hitmap = {}
        for chan in np.unique(channels):
            mask = (channels == chan).astype(bool)
            hitmap[chan] = event.Photons(pos[mask], dir[mask], pol[mask], wavelengths[mask], t[mask], last_hit_triangles[mask], flags[mask], weights[mask])
        return hitmap

    def iterate_copies(self):
        '''Returns an iterator that yields GPUPhotonsSlice objects
        corresponding to the event copies stored in ``self``.'''
        for i in xrange(self.ncopies):
            window = slice(self.true_nphotons*i, self.true_nphotons*(i+1))
            yield GPUPhotonsSlice(pos=self.pos[window],
                                  dir=self.dir[window],
                                  pol=self.pol[window],
                                  wavelengths=self.wavelengths[window],
                                  t=self.t[window],
                                  last_hit_triangles=self.last_hit_triangles[window],
                                  flags=self.flags[window],
                                  weights=self.weights[window])

    @profile_if_possible
    def propagate(self, gpu_geometry, rng_states, nthreads_per_block=64,
                  max_blocks=1024, max_steps=10, use_weights=False,
                  scatter_first=0):
        """Propagate photons on GPU to termination or max_steps, whichever
        comes first.

        May be called repeatedly without reloading photon information if
        single-stepping through photon history.

        ..warning::
            `rng_states` must have at least `nthreads_per_block`*`max_blocks`
            number of curandStates.
        """
        nphotons = self.pos.size
        step = 0
        input_queue = np.empty(shape=nphotons+1, dtype=np.uint32)
        input_queue[0] = 0
        # Order photons initially in the queue to put the clones next to each other
        for copy in xrange(self.ncopies):
            input_queue[1+copy::self.ncopies] = np.arange(self.true_nphotons, dtype=np.uint32) + copy * self.true_nphotons
        input_queue_gpu = ga.to_gpu(input_queue)
        output_queue = np.zeros(shape=nphotons+1, dtype=np.uint32)
        output_queue[0] = 1
        output_queue_gpu = ga.to_gpu(output_queue)

        while step < max_steps:
            # Just finish the rest of the steps if the # of photons is low
            if nphotons < nthreads_per_block * 16 * 8 or use_weights:
                nsteps = max_steps - step
            else:
                nsteps = 1

            for first_photon, photons_this_round, blocks in \
                    chunk_iterator(nphotons, nthreads_per_block, max_blocks):
                self.gpu_funcs.propagate(np.int32(first_photon), np.int32(photons_this_round), input_queue_gpu[1:], output_queue_gpu, rng_states, self.pos, self.dir, self.wavelengths, self.pol, self.t, self.flags, self.last_hit_triangles, self.weights, np.int32(nsteps), np.int32(use_weights), np.int32(scatter_first), gpu_geometry.gpudata, block=(nthreads_per_block,1,1), grid=(blocks, 1))

            step += nsteps
            scatter_first = 0 # Only allow non-zero in first pass

            if step < max_steps:
                temp = input_queue_gpu
                input_queue_gpu = output_queue_gpu
                output_queue_gpu = temp
                # Assign with a numpy array of length 1 to silence
                # warning from PyCUDA about setting array with different strides/storage orders.
                output_queue_gpu[:1].set(np.ones(shape=1, dtype=np.uint32))
                nphotons = input_queue_gpu[:1].get()[0] - 1

        if ga.max(self.flags).get() & (1 << 31):
            print >>sys.stderr, "WARNING: ABORTED PHOTONS"
        cuda.Context.get_current().synchronize()


    @profile_if_possible
    def select(self, target_flag, nthreads_per_block=64, max_blocks=1024,
               start_photon=None, nphotons=None):
        '''Return a new GPUPhoton object containing only photons that
        have a particular bit set in their history word.'''
        cuda.Context.get_current().synchronize()
        index_counter_gpu = ga.zeros(shape=1, dtype=np.uint32)
        cuda.Context.get_current().synchronize()
        if start_photon is None:
            start_photon = 0
        if nphotons is None:
            nphotons = self.pos.size - start_photon

        # First count how much space we need
        for first_photon, photons_this_round, blocks in \
                chunk_iterator(nphotons, nthreads_per_block, max_blocks):
            self.gpu_funcs.count_photons(np.int32(start_photon+first_photon), 
                                         np.int32(photons_this_round),
                                         np.uint32(target_flag),
                                         index_counter_gpu, self.flags,
                                         block=(nthreads_per_block,1,1), 
                                         grid=(blocks, 1))
        cuda.Context.get_current().synchronize()
        reduced_nphotons = int(index_counter_gpu.get()[0])
        # Then allocate new storage space
        pos = ga.empty(shape=reduced_nphotons, dtype=ga.vec.float3)
        dir = ga.empty(shape=reduced_nphotons, dtype=ga.vec.float3)
        pol = ga.empty(shape=reduced_nphotons, dtype=ga.vec.float3)
        wavelengths = ga.empty(shape=reduced_nphotons, dtype=np.float32)
        t = ga.empty(shape=reduced_nphotons, dtype=np.float32)
        last_hit_triangles = ga.empty(shape=reduced_nphotons, dtype=np.int32)
        flags = ga.empty(shape=reduced_nphotons, dtype=np.uint32)
        weights = ga.empty(shape=reduced_nphotons, dtype=np.float32)

        # And finaly copy photons, if there are any
        if reduced_nphotons > 0:
            index_counter_gpu.fill(0)
            for first_photon, photons_this_round, blocks in \
                    chunk_iterator(nphotons, nthreads_per_block, max_blocks):
                self.gpu_funcs.copy_photons(np.int32(start_photon+first_photon), 
                                            np.int32(photons_this_round), 
                                            np.uint32(target_flag),
                                            index_counter_gpu, 
                                            self.pos, self.dir, self.wavelengths, self.pol, self.t, self.flags, self.last_hit_triangles, self.weights,
                                            pos, dir, wavelengths, pol, t, flags, last_hit_triangles, weights,
                                            block=(nthreads_per_block,1,1), 
                                            grid=(blocks, 1))
            assert index_counter_gpu.get()[0] == reduced_nphotons
        return GPUPhotonsSlice(pos, dir, pol, wavelengths, t, last_hit_triangles, flags, weights)

    def __del__(self):
        del self.pos
        del self.dir
        del self.pol
        del self.wavelengths
        del self.t
        del self.flags
        del self.last_hit_triangles
        # Free up GPU memory quickly if now available
        gc.collect()


    def __len__(self):
        return self.pos.size

class GPUPhotonsSlice(GPUPhotons):
    '''A `slice`-like view of a subrange of another GPU photons array.
    Works exactly like an instance of GPUPhotons, but the GPU storage
    is taken from another GPUPhotons instance.

    Returned by the GPUPhotons.iterate_copies() iterator.'''
    def __init__(self, pos, dir, pol, wavelengths, t, last_hit_triangles,
                 flags, weights):
        '''Create new object using slices of GPUArrays from an instance
        of GPUPhotons.  NOTE THESE ARE NOT CPU ARRAYS!'''
        self.pos = pos
        self.dir = dir
        self.pol = pol
        self.wavelengths = wavelengths
        self.t = t
        self.last_hit_triangles = last_hit_triangles
        self.flags = flags
        self.weights = weights

        module = get_cu_module('propagate.cu', options=cuda_options)
        self.gpu_funcs = GPUFuncs(module)

        self.true_nphotons = len(pos)
        self.ncopies = 1

    def __del__(self):
        pass # Do nothing, because we don't own any of our GPU memory
