#!/usr/bin/env python
# coding: utf-8

# In[1]:


import os
import fluordynamics as fd
import matplotlib.pyplot as plt
path = os.path.dirname(fd.__file__)

experiment = fd.fluorburst.Experiment.load(path+'/../docs/source/tutorial/trajectory_examples/dna')

f, ax = plt.subplots(nrows=1, ncols=1, figsize=(2.5, 2), sharex=False, sharey=False, squeeze=False)
plt.hist(experiment.FRETefficiencies,bins=20, range=(0,1), color='grey', edgecolor='black')
ax[0,0].set_xlabel('FRET')
ax[0,0].set_ylabel('occupancy')

